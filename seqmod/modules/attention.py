
import torch
import torch.nn as nn
import torch.nn.functional as F


def DotScorer(dec_out, enc_outs, **kwargs):
    """
    Score for query decoder state and the ith encoder state is given
    by their dot product.
    """
    return torch.bmm(enc_outs.transpose(0, 1), dec_out.unsqueeze(2)).squeeze(2)


class GeneralScorer(nn.Module):
    """
    Inserts a linear projection to the query state before the dot product
    """
    def __init__(self, dim):
        super(GeneralScorer, self).__init__()

        self.W_a = nn.Linear(dim, dim, bias=False)

    def forward(self, dec_out, enc_outs, **kwargs):
        return DotScorer(self.W_a(dec_out), enc_outs)


class BahdanauScorer(nn.Module):
    """
    Projects both query decoder state and encoder states to an attention space.
    The scores are computed by a dot product with a learnable param v_a after
    transforming the sum of query decoder state and encoder state with a tanh.

    `score(a_i_j) = a_v \dot tanh(W_s @ h_s_j + W_t @ h_t_i)`
    """
    def __init__(self, hid_dim1, att_dim, hid_dim2=None):
        super(BahdanauScorer, self).__init__()
        # params
        hid_dim2 = hid_dim2 or hid_dim1
        self.W_s = nn.Linear(hid_dim1, att_dim, bias=False)
        self.W_t = nn.Linear(hid_dim2, att_dim, bias=True)
        self.v_a = nn.Parameter(torch.Tensor(att_dim, 1))
        self.v_a.data.uniform_(-0.05, 0.05)
        self.v_a.custom = True  # don't overwrite initialization

    def project_enc_outs(self, enc_outs):
        """
        mapping: (seq_len x batch x hid_dim) -> (seq_len x batch x att_dim)

        Returns:
        --------
        torch.Tensor (seq_len x batch x att_dim),
            Projection of encoder output onto attention space
        """
        seq_len, batch, hid_dim = enc_outs.size()
        return self.W_s(enc_outs.view(-1, hid_dim)).view(seq_len, batch, -1)

    def forward(self, dec_out, enc_outs, enc_att=None):
        if enc_att is None:
            # (seq_len x batch x att dim)
            enc_att = self.project_enc_outs(enc_outs)
        # (batch x att_dim)
        dec_att = self.W_t(dec_out)
        # (batch x seq_len x att_dim)
        dec_enc_att = F.tanh(enc_att + dec_att[None, :, :])
        # (batch x seq_len x att_dim) * (1 x att_dim x 1) -> (batch x seq_len)
        return (dec_enc_att.transpose(0, 1) @ self.v_a[None, :, :]).squeeze(2)


class Attention(nn.Module):
    """
    Global attention implementing the three scorer modules from Luong 15.

    Parameters:
    -----------
    - hid_dim: int, dimensionality of the query vector
    - att_dim: (optional) int, dimensionality of the attention space (only
        used by the bahdanau scorer). If not given it will default to hid_dim.
    - scorer: str, one of ('dot', 'general', 'bahdanau')
    - hid_dim2: (optional), int, dimensionality of the key vectors (optionally
        used by the bahdanau scorer if given)
    """
    def __init__(self, hid_dim, att_dim=None, scorer='general', hid_dim2=None):
        super(Attention, self).__init__()

        if scorer != 'bahdanau' and att_dim is not None and hid_dim != hid_dim:
            raise ValueError("Global attention requires attention size "
                             "equal to Encoder/Decoder hidden size")
        if scorer == 'bahdanau:' and att_dim is None:
            att_dim = hid_dim

        # Scorer
        if scorer.lower() == 'dot':
            self.scorer = DotScorer
        elif scorer.lower() == 'general':
            self.scorer = GeneralScorer(hid_dim)
        elif scorer.lower() == 'bahdanau':
            self.scorer = BahdanauScorer(hid_dim, att_dim, hid_dim2=hid_dim2)
        else:
            raise ValueError(
                "`scorer` must be one of ('dot', 'general', 'bahdanau')"
                " but got {}".format(scorer))

        # Output layer (Luong 15. eq (5))
        self.linear_out = nn.Linear(
            hid_dim * 2, hid_dim, bias=scorer.lower() == 'bahdanau')

    def forward(self, dec_out, enc_outs, enc_att=None, mask=None):
        """
        Parameters:
        -----------

        - dec_out: torch.Tensor(batch_size x hid_dim)
        - enc_outs: torch.Tensor(seq_len x batch_size x hid_dim)
        - enc_att: (optional), torch.Tensor(seq_len x batch_size x att_dim)
        """
        # (batch x seq_len)
        weights = self.scorer(dec_out, enc_outs, enc_att=enc_att)

        if mask is not None:
            # weights = weights * mask.float()
            weights.data.masked_fill_(1 - mask.data, -float('inf'))

        weights = F.softmax(weights, dim=1)

        # (eq 7)
        context = weights.unsqueeze(1).bmm(enc_outs.transpose(0, 1)).squeeze(1)
        # (eq 5) linear out combining context and hidden
        context = F.tanh(self.linear_out(torch.cat([context, dec_out], 1)))

        return context, weights
