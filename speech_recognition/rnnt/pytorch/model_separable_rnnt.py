from typing import Optional, Tuple

import numpy as np
import torch
import os

from rnn import rnn
from rnn import StackTime

import rnnt_logging

class RNNT(torch.nn.Module):
    def __init__(self, rnnt=None, num_classes=1, **kwargs):
        super().__init__()
        if kwargs.get("no_featurizer", False):
            in_features = kwargs.get("in_features")
        else:
            feat_config = kwargs.get("feature_config")
            # This may be useful in the future, for MLPerf
            # configuration.
            in_features = feat_config['features'] * \
                feat_config.get("frame_splicing", 1)

        if kwargs.get("instr", False):
           self.instr = rnnt_logging.Logging()
        else:
           self.instr = rnnt_logging.DummyLogging()

        self.encoder = Encoder(in_features,
            rnnt["encoder_n_hidden"],
            rnnt["encoder_pre_rnn_layers"],
            rnnt["encoder_post_rnn_layers"],
            rnnt["forget_gate_bias"],
            None if "norm" not in rnnt else rnnt["norm"],
            rnnt["rnn_type"],
            rnnt["encoder_stack_time_factor"],
            rnnt["dropout"],
            self.instr
        )

        self.prediction = Prediction(
            num_classes,
            rnnt["pred_n_hidden"],
            rnnt["pred_rnn_layers"],
            rnnt["forget_gate_bias"],
            None if "norm" not in rnnt else rnnt["norm"],
            rnnt["rnn_type"],
            rnnt["dropout"],
            self.instr
        )

        self.joint = Joint(
            num_classes,
            rnnt["pred_n_hidden"],
            rnnt["encoder_n_hidden"],
            rnnt["joint_n_hidden"],
            rnnt["dropout"],
        )

    def forward(self, x_padded: torch.Tensor, x_lens: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        return self.encoder(x_padded, x_lens)


class DumpRNN(torch.nn.Module):
    def __init__(self, fn, prefix, instr):
        super().__init__()
        self.fn = fn
        self.prefix = prefix
        self.count = 0
        self.instr = instr
        self.filename = instr.dumping.get_filename()

    def forward(self, in_padded: torch.Tensor, in_lens: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        # if the filename changes, reset the counter
        if self.instr.dumping.get_filename() != self.filename:
            self.filename = self.instr.dumping.get_filename()
            self.count = 0
        out_padded, out_lens = self.fn(in_padded, in_lens)
        torch.save((in_padded, in_lens, out_padded, out_lens), self.filename + '-' + self.prefix + str(self.count).zfill(4) + ".pt")
        self.count += 1
        return out_padded, out_lens

    __call__ = forward

class Encoder(torch.nn.Module):
    def __init__(self, in_features, encoder_n_hidden,
                 encoder_pre_rnn_layers, encoder_post_rnn_layers,
                 forget_gate_bias, norm, rnn_type, encoder_stack_time_factor,
                 dropout, instr):
        super().__init__()
        self.pre_rnn = rnn(
            rnn=rnn_type,
            input_size=in_features,
            hidden_size=encoder_n_hidden,
            num_layers=encoder_pre_rnn_layers,
            norm=norm,
            forget_gate_bias=forget_gate_bias,
            dropout=dropout,
        )
        self.stack_time = StackTime(factor=encoder_stack_time_factor)
        self.post_rnn = rnn(
            rnn=rnn_type,
            input_size=encoder_stack_time_factor * encoder_n_hidden,
            hidden_size=encoder_n_hidden,
            num_layers=encoder_post_rnn_layers,
            norm=norm,
            forget_gate_bias=forget_gate_bias,
            norm_first_rnn=True,
            dropout=dropout,
        )

        # if there are plugins available, load them
        try:
            from lstm_rnnt_pre import PluginLstmRnntPre
            self.call_pre_rnn = PluginLstmRnntPre()
        except:
            self.call_pre_rnn = self.pre_rnn

        try:
            from lstm_rnnt_post import PluginLstmRnntPost
            self.call_post_rnn = PluginLstmRnntPost()
        except:
            self.call_post_rnn = self.post_rnn

        # enable dumping if required
        dump_pre = os.environ.get('CK_RNNT_DUMP_PRE', 'no') in [ 'yes', 'YES', 'ON', 'on', '1' ]
        self.call_pre_rnn = (self.call_pre_rnn if not dump_pre else DumpRNN( self.call_pre_rnn, 'PRE', instr ))
        dump_post = os.environ.get('CK_RNNT_DUMP_POST', 'no') in [ 'yes', 'YES', 'ON', 'on', '1' ]
        self.call_post_rnn = (self.call_post_rnn if not dump_post else DumpRNN( self.call_post_rnn, 'POST', instr ))

        self.instr = instr


    def forward(self, x_padded: torch.Tensor, x_lens: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        self.instr.log_pre_start()
        x_padded, _ = self.call_pre_rnn(x_padded, None)
        self.instr.log_pre_end()
        x_padded, x_lens = self.stack_time(x_padded, x_lens)
        # (T, B, H)
        self.instr.log_post_start()
        x_padded, _ = self.call_post_rnn(x_padded, None)
        self.instr.log_post_end()
        # (B, T, H)
        x_padded = x_padded.transpose(0, 1)
        return x_padded, x_lens

class Prediction(torch.nn.Module):
    def __init__(self, vocab_size, n_hidden, pred_rnn_layers,
                 forget_gate_bias, norm, rnn_type, dropout, instr):
        super().__init__()
        self.embed = torch.nn.Embedding(vocab_size - 1, n_hidden)
        self.n_hidden = n_hidden
        self.dec_rnn = rnn(
            rnn=rnn_type,
            input_size=n_hidden,
            hidden_size=n_hidden,
            num_layers=pred_rnn_layers,
            norm=norm,
            forget_gate_bias=forget_gate_bias,
            dropout=dropout,
        )

        # if there is a plugin available, load it
        try:
            from lstm_rnnt_dec import PluginLstmRnntDec
            self.call_dec_rnn = PluginLstmRnntDec()
        except:
            self.call_dec_rnn = self.dec_rnn

        # enable dumping if required
        dump_dec = os.environ.get('CK_RNNT_DUMP_DEC', 'no') in [ 'yes', 'YES', 'ON', 'on', '1' ]
        self.call_dec_rnn = (self.call_dec_rnn if not dump_dec else DumpRNN( self.call_dec_rnn, 'DEC', instr))

        # setup instrumentation
        self.instr = instr


    def forward(self, y: Optional[torch.Tensor],
                state: Optional[Tuple[torch.Tensor, torch.Tensor]] = None) -> Tuple[torch.Tensor, Tuple[torch.Tensor, torch.Tensor]]:
        """
        B - batch size
        U - label length
        H - Hidden dimension size
        L - Number of decoder layers = 2

        Args:
            y: (B, U)

        Returns:
            Tuple (g, hid) where:
                g: (B, U + 1, H)
                hid: (h, c) where h is the final sequence hidden state and c is
                    the final cell state:
                        h (tensor), shape (L, B, H)
                        c (tensor), shape (L, B, H)
        """
        if y is None:
            # This is gross. I should really just pass in an SOS token
            # instead. Is there no SOS token?
            assert state is None
            # Hacky, no way to determine this right now!
            B = 1
            y = torch.zeros((B, 1, self.n_hidden), dtype=torch.float32)
        else:
            y = self.embed(y)

        # if state is None:
        #    batch = y.size(0)
        #    state = [
        #        (torch.zeros(batch, self.pred_n_hidden, dtype=y.dtype, device=y.device),
        #         torch.zeros(batch, self.pred_n_hidden, dtype=y.dtype, device=y.device))
        #        for _ in range(self.pred_rnn_layers)
        #    ]

        y = y.transpose(0, 1)  # .contiguous()   # (U + 1, B, H)
        self.instr.log_dec_start()
        g, hid = self.call_dec_rnn(y, state)
        self.instr.log_dec_end()
        g = g.transpose(0, 1)  # .contiguous()   # (B, U + 1, H)
        # del y, state
        return g, hid

class Joint(torch.nn.Module):
    def __init__(self, vocab_size, pred_n_hidden, enc_n_hidden,
                 joint_n_hidden, dropout):
        super().__init__()
        layers = [
            torch.nn.Linear(pred_n_hidden + enc_n_hidden, joint_n_hidden),
            torch.nn.ReLU(),
        ] + ([torch.nn.Dropout(p=dropout), ] if dropout else []) + [
            torch.nn.Linear(joint_n_hidden, vocab_size)
        ]
        self.net = torch.nn.Sequential(
            *layers
        )

    def forward(self, f: torch.Tensor, g: torch.Tensor):
        """
        f should be shape (B, T, H)
        g should be shape (B, U + 1, H)

        returns:
            logits of shape (B, T, U, K + 1)
        """
        # Combine the input states and the output states
        B, T, H = f.shape
        B, U_, H2 = g.shape

        f = f.unsqueeze(dim=2)   # (B, T, 1, H)
        f = f.expand((B, T, U_, H))

        g = g.unsqueeze(dim=1)   # (B, 1, U + 1, H)
        g = g.expand((B, T, U_, H2))

        inp = torch.cat([f, g], dim=3)   # (B, T, U, 2H)
        res = self.net(inp)
        # del f, g, inp
        return res

def label_collate(labels):
    """Collates the label inputs for the rnn-t prediction network.

    If `labels` is already in torch.Tensor form this is a no-op.

    Args:
        labels: A torch.Tensor List of label indexes or a torch.Tensor.

    Returns:
        A padded torch.Tensor of shape (batch, max_seq_len).
    """

    if isinstance(labels, torch.Tensor):
        return labels.type(torch.int64)
    if not isinstance(labels, (list, tuple)):
        raise ValueError(
            f"`labels` should be a list or tensor not {type(labels)}"
        )

    batch_size = len(labels)
    max_len = max(len(l) for l in labels)

    cat_labels = np.full((batch_size, max_len), fill_value=0.0, dtype=np.int32)
    for e, l in enumerate(labels):
        cat_labels[e, :len(l)] = l
    labels = torch.LongTensor(cat_labels)

    return labels