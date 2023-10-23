"""RWKV encoder definition for Transducer models."""

import math
from typing import Dict, List, Optional, Tuple

import torch

from funasr.models.encoder.abs_encoder import AbsEncoder
from funasr.modules.rwkv import RWKV
from funasr.modules.layer_norm import LayerNorm
from funasr.modules.rwkv_subsampling import RWKVConvInput
from funasr.modules.nets_utils import make_source_mask

class RWKVEncoder(AbsEncoder):
    """RWKV encoder module.

    Based on https://arxiv.org/pdf/2305.13048.pdf.

    Args:
        vocab_size: Vocabulary size.
        output_size: Input/Output size.
        context_size: Context size for WKV computation.
        linear_size: FeedForward hidden size.
        attention_size: SelfAttention hidden size.
        normalization_type: Normalization layer type.
        normalization_args: Normalization layer arguments.
        num_blocks: Number of RWKV blocks.
        embed_dropout_rate: Dropout rate for embedding layer.
        att_dropout_rate: Dropout rate for the attention module.
        ffn_dropout_rate: Dropout rate for the feed-forward module.
    """

    def __init__(
        self,
        input_size: int,
        output_size: int = 512,
        context_size: int = 1024,
        linear_size: Optional[int] = None,
        attention_size: Optional[int] = None,
        num_blocks: int = 4,
        att_dropout_rate: float = 0.0,
        ffn_dropout_rate: float = 0.0,
        dropout_rate: float = 0.0,
        subsampling_factor: int =4,
        time_reduction_factor: int = 1,
        kernel: int = 3,
    ) -> None:
        """Construct a RWKVEncoder object."""
        super().__init__()

        self.embed = RWKVConvInput(
            input_size,
            [output_size//4, output_size//2, output_size],
            subsampling_factor,
            conv_kernel_size=kernel,
            output_size=output_size,
        )

        self.subsampling_factor = subsampling_factor

        linear_size = output_size * 4 if linear_size is None else linear_size
        attention_size = output_size if attention_size is None else attention_size
        
        self.rwkv_blocks = torch.nn.ModuleList(
            [
                RWKV(
                    output_size,
                    linear_size,
                    attention_size,
                    context_size,
                    block_id,
                    num_blocks,
                    att_dropout_rate=att_dropout_rate,
                    ffn_dropout_rate=ffn_dropout_rate,
                    dropout_rate=dropout_rate,
                )
                for block_id in range(num_blocks)
            ]
        )

        self.embed_norm = LayerNorm(output_size)
        self.final_norm = LayerNorm(output_size)

        self._output_size = output_size
        self.context_size = context_size

        self.num_blocks = num_blocks
        self.time_reduction_factor = time_reduction_factor

    def output_size(self) -> int:
        return self._output_size

    def forward(self, x: torch.Tensor, x_len) -> torch.Tensor:
        """Encode source label sequences.

        Args:
            x: Encoder input sequences. (B, L)

        Returns:
            out: Encoder output sequences. (B, U, D)

        """
        _, length, _ = x.size()

        assert (
            length <= self.context_size * self.subsampling_factor
        ), "Context size is too short for current length: %d versus %d" % (
            length,
            self.context_size * self.subsampling_factor,
        )
        mask = make_source_mask(x_len).to(x.device)
        x, mask = self.embed(x, mask, None)
        x = self.embed_norm(x)
        olens = mask.eq(0).sum(1)

        for block in self.rwkv_blocks:
            x, _ = block(x)
        # for streaming inference
        # xs_pad = self.rwkv_infer(xs_pad)

        x = self.final_norm(x)

        if self.time_reduction_factor > 1:
            x = x[:,::self.time_reduction_factor,:]
            olens = torch.floor_divide(olens-1, self.time_reduction_factor) + 1

        return x, olens, None

    def rwkv_infer(self, xs_pad):

        batch_size = xs_pad.shape[0]

        hidden_sizes = [
            self._output_size for i in range(5)
        ]

        state = [
            torch.zeros(
                (batch_size, 1, hidden_sizes[i], self.num_rwkv_blocks),
                dtype=torch.float32,
                device=self.device,
            )
            for i in range(5)
        ]

        state[4] -= 1e-30

        xs_out = []
        for t in range(xs_pad.shape[1]):
            x_t = xs_pad[:,t,:]
            for idx, block in enumerate(self.rwkv_blocks):
                x_t, state = block(x_t, state=state)
            xs_out.append(x_t)
        xs_out = torch.stack(xs_out, dim=1)
        return xs_out
