
import math
import logging

import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

import seqmod.utils as u
from seqmod.modules.custom import word_dropout, StackedLSTM, StackedGRU, Highway
from seqmod.modules.encoder_decoder import Encoder


def generic_sigmoid(a=1, b=1, c=1):
    return lambda x: a / (1 + b * math.exp(-x * c))


def kl_sigmoid_annealing_schedule(inflection, steepness=2):
    b = 10 ** steepness
    return generic_sigmoid(b=b, c=math.log(b) / inflection)


def kl_linear_annealing_schedule(max_steps):
    return lambda x: x / max_steps   # TODO: return interpolation


# KL-divergence loss
def KL_loss(mu, logvar):
    """
    https://arxiv.org/abs/1312.6114
    0.5 * sum(1 + log(sigma^2) - mu^2 - sigma^2)
    """
    element = mu.pow(2).add_(logvar.exp()).mul_(-1).add_(1).add_(logvar)
    return torch.sum(element).mul_(-0.5)


class EncoderVAE(Encoder):
    def __init__(self, z_dim, *args, **kwargs):
        super(EncoderVAE, self).__init__(*args, **kwargs)
        # dimension of the hidden output of the encoder
        self.enc_dim = self.hid_dim * self.num_dirs * 2  # concat with average
        self.z_dim = z_dim
        self.Q_mu = nn.Linear(self.enc_dim, self.z_dim)
        self.Q_logvar = nn.Linear(self.enc_dim, self.z_dim)

    def reparametrize(self, mu, logvar):
        """
        z = mu + eps *. sqrt(exp(log(s^2)))

        The second term obtains interpreting logvar as the log-var of z
        and observing that sqrt(exp(s^2)) == exp(s^2/2)
        """
        eps = Variable(logvar.data.new(*logvar.size()).normal_())
        std = logvar.mul(0.5).exp_()
        return eps.mul(std).add_(mu)

    def forward(self, inp, hidden=None, **kwargs):
        out, _ = super(EncoderVAE, self).forward(inp, hidden, **kwargs)
        context = torch.cat([out[:-1].mean(0), out[-1]], 1)
        mu, logvar = self.Q_mu(context), self.Q_logvar(context)
        return mu, logvar


class DecoderVAE(nn.Module):
    def __init__(self, z_dim, emb_dim, hid_dim, num_layers=1, cell='LSTM',
                 dropout=0.0, maxout=0, add_z=False, project_init=False):
        in_dim = emb_dim if not add_z else z_dim + emb_dim
        self.z_dim = z_dim
        self.emb_dim = emb_dim
        self.hid_dim = hid_dim
        self.cell = cell
        self.num_layers = num_layers
        self.dropout = dropout
        self.add_z = add_z
        self.project_init = project_init
        assert project_init or (num_layers * hid_dim == z_dim), \
            "Cannot interpret z as initial hidden state. Use project_init."
        super(DecoderVAE, self).__init__()

        # project_init
        if self.project_init:
            self.hid2z_proj = nn.Linear(z_dim, self.hid_dim * self.num_layers)

        # add highway projection
        if self.add_z:
            self.z_proj = Highway(z_dim, num_layers=2)  # add option

        # rnn
        stacked = StackedLSTM if self.cell == 'LSTM' else StackedGRU
        self.rnn_step = stacked(
            self.num_layers, in_dim, self.hid_dim, dropout=dropout)

    def init_hidden_for(self, z):
        batch = z.size(0)

        if self.project_init:
            h_0 = self.hid2z_proj(z)
            h_0 = h_0.view(batch, self.num_layers, self.hid_dim).transpose(0, 1)
        else:                 # rearrange z to match hidden cell shape
            h_0 = z.view(self.num_layers, batch, self.hid_dim)

        if self.cell.startswith('LSTM'):
            # compute memory cell
            c_0 = z.data.new(self.num_layers, batch, self.hid_dim)
            c_0 = Variable(nn.init.xavier_uniform(c_0))
            return h_0, c_0
        else:
            return h_0

    def forward(self, prev, hidden, z=None):
        """
        Parameters:
        -----------
        prev: (batch x emb_dim). Conditioning item at current step.
        hidden: (batch x hid_dim). Hidden state at current step.
        z: None or (batch x z_dim). Latent code for the input sequence.
            If it is provided, it will be used to condition the generation
            of each item in the sequence.
        """
        if self.add_z:
            assert z is not None, "z must be given when add_z is set to True"
            inp = torch.cat([prev, self.z_proj(z)], 1)
        else:
            inp = prev
        out, hidden = self.rnn_step(inp, hidden)
        out = F.dropout(out, p=self.dropout, training=self.training)
        return out, hidden


class SequenceVAE(nn.Module):
    def __init__(self, emb_dim, hid_dim, z_dim, src_dict, num_layers=1,
                 cell='LSTM', bidi=True, dropout=0.0, word_dropout=0.0,
                 project_init=False, add_z=False, tie_weights=False,
                 kl_schedule=kl_sigmoid_annealing_schedule(inflection=5000)):
        self.hid_dim, self.num_layers = hid_dim, num_layers
        self.add_z = add_z
        self.src_dict = src_dict
        vocab_size = len(src_dict)
        super(SequenceVAE, self).__init__()

        # Training stuff
        self.nll_weight = torch.ones(vocab_size)
        self.nll_weight[self.src_dict.get_pad()] = 0

        self.kl_weight = 0.0
        self.kl_schedule = kl_schedule

        # Word_dropout
        self.word_dropout = word_dropout
        self.target_code = self.src_dict.get_unk()
        self.reserved_codes = (self.src_dict.get_bos(),
                               self.src_dict.get_eos(),
                               self.src_dict.get_pad())

        # Embedding layer(s)
        self.embeddings = nn.Embedding(
            vocab_size, emb_dim, padding_idx=self.src_dict.get_pad())

        # Encoder
        self.encoder = EncoderVAE(
            z_dim, emb_dim, hid_dim, num_layers,
            cell=cell, bidi=bidi, dropout=dropout)

        # Decoder
        self.decoder = DecoderVAE(
            z_dim, emb_dim, hid_dim, num_layers=num_layers, cell=cell,
            project_init=project_init, add_z=add_z, dropout=dropout)

        # Projection
        if tie_weights:
            projection = nn.Linear(emb_dim, vocab_size)
            projection.weight = self.embeddings.weight
            if emb_dim != hid_dim:
                logging.warn("When tying weights, output layer and "
                             "embedding layer should have equal size. "
                             "A projection layer will be insterted.")
                tied_projection = nn.Linear(hid_dim, emb_dim)
                self.out_proj = nn.Sequential(tied_projection, projection)
            else:
                self.out_proj = projection
        else:
            self.out_proj = nn.Linear(hid_dim, vocab_size)

    def is_cuda(self):
        return next(self.parameters()).is_cuda

    def init_embeddings(self, weight):
        emb_elements = self.embeddings.weight.data.nelement()
        mismatch_msg = "Expected " + str(emb_elements) + "elements but got {}"

        if isinstance(weight, np.ndarray):
            assert emb_elements == weight.size, \
                mismatch_msg.format(weight.size)
            self.embeddings.weight.data = torch.Tensor(weight)

        elif isinstance(weight, torch.Tensor):
            assert emb_elements == weight.nelement(), \
                mismatch_msg.format(weight.nelement())
            self.embeddings.weight.data = weight

        elif isinstance(weight, nn.Parameter):
            assert emb_elements == weight.nelement(), \
                mismatch_msg.format(weight.nelement())
            self.embeddings.weight = weight

        else:
            raise ValueError("Unknown weight type [{}]".format(type(weight)))

    def project(self, dec_outs):
        """
        Parameters:
        -----------
        dec_outs: (seq_len x batch x hid_dim), decoder output

        Returns: dec_logs (seq_len * batch x vocab_size)
        """
        seq_len, batch, hid_dim = dec_outs.size()
        dec_outs = self.out_proj(dec_outs.view(batch * seq_len, hid_dim))
        dec_logs = F.log_softmax(dec_outs)
        return dec_logs

    def forward(self, src, trg, labels=None):
        """
        Parameters:
        -----------

        inp: (seq_len x batch). Input batch of sentences to be encoded.
            It is assumed that inp has <bos> and <eos> symbols.
        labels: None or (batch x num_labels). To be used by conditional VAEs.

        Returns:
        --------
        preds: (batch x vocab_size * seq_len)
        mu: (batch x z_dim)
        logvar: (batch x z_dim)
        """
        # - encoder
        emb = self.embeddings(src)
        mu, logvar = self.encoder(emb)
        z = self.encoder.reparametrize(mu, logvar)
        # - decoder
        hidden, dec_outs = self.decoder.init_hidden_for(z), []
        # apply word dropout on the conditioning targets
        trg = word_dropout(
            trg, self.target_code, p=self.word_dropout,
            reserved_codes=self.reserved_codes, training=self.training)
        for emb_t in self.embeddings(trg).chunk(trg.size(0)):
            # rnn
            dec_out, hidden = self.decoder(emb_t.squeeze(0), hidden, z=z)
            dec_outs.append(dec_out)

        dec_outs = self.project(torch.stack(dec_outs))

        return dec_outs, mu, logvar

    def loss(self, batch_data, test=False):
        """
        Compute loss, eventually backpropagate and return losses and batch size
        for speed monitoring
        """
        pad, eos = self.src_dict.get_pad(), self.src_dict.get_eos()
        src, _ = batch_data

        # remove <eos> from decoder targets dealing with different <pad> sizes
        dec_trg = Variable(u.map_index(src[:-1].data, eos, pad))
        # remove <bos> from loss targets
        loss_trg = src[1:].view(-1)
        # preds (batch * seq_len x vocab)
        preds, mu, logvar = self(src, dec_trg)

        # compute loss
        weight = self.nll_weight
        if next(self.parameters()).is_cuda:
            weight = weight.cuda()

        rec_examples, kl_examples = src.data.ne(pad).int().sum(), src.size(1)
        log_loss = F.nll_loss(
            preds, loss_trg, size_average=False, weight=weight) / rec_examples
        kl_loss = KL_loss(mu, logvar) / rec_examples  # kl_examples

        if not test:
            (log_loss + self.kl_weight * kl_loss).backward()

        return (log_loss.data[0], kl_loss.data[0]), rec_examples

    def generate(self, inp=None, z_params=None, beam_width=5,
                 max_inp_len=2, max_len=20, **kwargs):
        """
        inp: None or (seq_len x batch_size). Input sequences to be encoded.
            It will be ignored if `z_params` is not None.
        z_params: None or tuple(mu, logvar). If given, decoding will be done
            from the latent code and `inp` will be ignored.
            - mu: (batch_size x z_dim)
            - logvar: (batch_size x z_dim)
        """
        if inp is None and z_params is None:
            raise ValueError("At least one of (inp, z_params) must be given")

        eos, bos = self.src_dict.get_eos(), self.src_dict.get_bos()

        # Encoder
        if z_params is None:
            mu, logvar = self.encoder(self.embeddings(inp))
            max_len, batch_size = len(inp) * max_inp_len, inp.size(1)
        else:
            mu, logvar = z_params
            max_len, batch_size = max_len, mu.size(0)

        # Sample from the hidden code
        z = self.encoder.reparametrize(mu, logvar)

        # Decoder
        scores, preds = 0, []
        mask = mu.data.new(batch_size).long() + 1

        hidden = self.decoder.init_hidden_for(z)
        prev_data = mu.data.new(batch_size).zero_().long() + bos
        prev = Variable(prev_data, volatile=True)

        for _ in range(max_len):
            prev_emb = self.embeddings(prev).squeeze(0)
            dec_out, hidden = self.decoder(prev_emb, hidden, z=z)
            dec_out = self.project(dec_out.unsqueeze(0))

            score, pred = dec_out.max(1)
            scores += score.squeeze().data
            preds.append(pred.squeeze().data)
            prev = pred

            mask = mask * (pred.squeeze().data[0] != eos)

            if mask.int().sum() == 0:
                break

        return scores.tolist(), torch.stack(preds).transpose(0, 1).tolist()
