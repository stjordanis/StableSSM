"""Minimal version of STABLESSM with extra options and features stripped out, for pedagogical
purposes."""

import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from einops import rearrange, repeat


class DropoutNd(nn.Module):
    def __init__(self, p: float = 0.5, tie=True, transposed=True):
        """
        tie: tie dropout mask across sequence lengths (Dropout1d/2d/3d)
        """
        super().__init__()
        if p < 0 or p >= 1:
            raise ValueError("dropout probability has to be in [0, 1), " "but got {}".format(p))
        self.p = p
        self.tie = tie
        self.transposed = transposed
        self.binomial = torch.distributions.binomial.Binomial(probs=1 - self.p)

    def forward(self, X):
        """X: (batch, dim, lengths...)."""
        if self.training:
            if not self.transposed:
                X = rearrange(X, "b ... d -> b d ...")
            # binomial = torch.distributions.binomial.Binomial(probs=1-self.p) # This is incredibly slow because of CPU -> GPU copying
            mask_shape = X.shape[:2] + (1,) * (X.ndim - 2) if self.tie else X.shape
            # mask = self.binomial.sample(mask_shape)
            mask = torch.rand(*mask_shape, device=X.device) < 1.0 - self.p
            X = X * mask * (1.0 / (1 - self.p))
            if not self.transposed:
                X = rearrange(X, "b d ... -> b ... d")
            return X
        return X


class STABLESSMKernel(nn.Module):
    """Generate convolution kernel from diagonal SSM parameters."""

    def __init__(self, d_model, N=64, dt_min=0.001, dt_max=0.1, lr=None, parameterization="exp"):
        super().__init__()

        self.parameterization = parameterization

        # Generate dt
        H = d_model
        log_dt = torch.rand(H) * (math.log(dt_max) - math.log(dt_min)) + math.log(dt_min)

        C = torch.randn(H, N // 2, dtype=torch.cfloat)
        self.C = nn.Parameter(torch.view_as_real(C))
        self.register("log_dt", log_dt, lr)

        # log_A_real = torch.log(0.5 * torch.ones(H, N // 2)) # Previous initialization of log_A_real
        A_weights = 0.5 * torch.ones(H, N // 2)
        A_imag = math.pi * repeat(torch.arange(N // 2), "n -> h n", h=H)
        if self.parameterization == "exp":
            log_A_real = torch.log(A_weights)
        elif self.parameterization == "softplus":
            log_A_real = torch.log(torch.expm1(A_weights))
        elif self.parameterization == "best":
            log_A_real = torch.sqrt(
                torch.maximum(1 / A_weights - 0.1, 1e-6 * torch.ones_like(A_weights))
            )
        elif self.parameterization == "direct":
            log_A_real = A_weights
        else:
            return ValueError(f"Unknown parameterization {self.parameterization}")
        self.register("log_A_real", log_A_real, lr)
        self.register("A_imag", A_imag, lr)

    def forward(self, L):
        """
        returns: (..., c, L) where c is number of channels (default 1)
        """

        # Materialize parameters
        dt = torch.exp(self.log_dt)  # (H)
        C = torch.view_as_complex(self.C)  # (H N)

        # Different parameterization is reflected here.
        if self.parameterization == "exp":
            A = -torch.exp(self.log_A_real) + 1j * self.A_imag
        elif self.parameterization == "softplus":
            A = -torch.log(1 + torch.exp(self.log_A_real)) + 1j * self.A_imag  # (H N)
        elif self.parameterization == "best":
            A = -1 / (self.log_A_real**2 + 0.1) + 1j * self.A_imag  # (H N)
        elif self.parameterization == "direct":
            A = self.log_A_real + 1j * self.A_imag  # (H N)
        else:
            return ValueError(f"Unknown parameterization {self.parameterization}")

        # The following is the time discretization
        # Vandermonde multiplication
        dtA = A * dt.unsqueeze(-1)  # (H N)
        K = dtA.unsqueeze(-1) * torch.arange(L, device=A.device)  # (H N L)
        C = C * (torch.exp(dtA) - 1.0) / A
        K = 2 * torch.einsum("hn, hnl -> hl", C, torch.exp(K)).real

        return K

    def register(self, name, tensor, lr=None):
        """Register a tensor with a configurable learning rate and 0 weight decay."""

        if lr == 0.0:
            self.register_buffer(name, tensor)
        else:
            self.register_parameter(name, nn.Parameter(tensor))

            optim = {"weight_decay": 0.0}
            if lr is not None:
                optim["lr"] = lr
            setattr(getattr(self, name), "_optim", optim)


class STABLESSM(nn.Module):
    def __init__(self, d_model, d_state=64, dropout=0.0, transposed=True, **kernel_args):
        super().__init__()

        self.h = d_model
        self.n = d_state
        self.d_output = self.h
        self.transposed = transposed

        self.D = nn.Parameter(torch.randn(self.h))

        # SSM Kernel
        self.kernel = STABLESSMKernel(self.h, N=self.n, **kernel_args)

        # Pointwise
        self.activation = nn.GELU()
        # dropout_fn = nn.Dropout2d # NOTE: bugged in PyTorch 1.11
        dropout_fn = DropoutNd
        self.dropout = dropout_fn(dropout) if dropout > 0.0 else nn.Identity()

        # position-wise output transform to mix features
        self.output_linear = nn.Sequential(
            nn.Conv1d(self.h, 2 * self.h, kernel_size=1),
            nn.GLU(dim=-2),
        )

    def forward(self, u, **kwargs):  # absorbs return_output and transformer src mask
        """Input and output shape (B, H, L)"""
        if not self.transposed:
            u = u.transpose(-1, -2)
        L = u.size(-1)

        # Compute SSM Kernel
        k = self.kernel(L=L)  # (H L)

        # Convolution
        k_f = torch.fft.rfft(k, n=2 * L)  # (H L)
        u_f = torch.fft.rfft(u, n=2 * L)  # (B H L)
        y = torch.fft.irfft(u_f * k_f, n=2 * L)[..., :L]  # (B H L)

        # Compute D term in state space equation - essentially a skip connection
        y = y + u * self.D.unsqueeze(-1)

        y = self.dropout(self.activation(y))
        y = self.output_linear(y)
        if not self.transposed:
            y = y.transpose(-1, -2)
        return (
            y,
            None,
        )  # Return a dummy state to satisfy this repo's interface, but this can be modified


class StableSSMModel(nn.Module):
    def __init__(
        self,
        rec1_size=256,
        n_layers=4,
        dropout=0.2,
        dt=0.33,
        prenorm=False,
        parameterization="exp",  # this is a kernel_arg
        return_seq: bool =False,
    ):
        super().__init__()

        self.prenorm = prenorm
        self.return_seq = return_seq

        # Stack StableSSM layers as residual blocks
        self.stablessm_layers = nn.ModuleList()
        self.norms = nn.ModuleList()
        self.dropouts = nn.ModuleList()
        for _ in range(n_layers):
            self.stablessm_layers.append(
                STABLESSM(
                    rec1_size,
                    dropout=dropout,
                    transposed=True,
                    lr=min(0.001, dt),
                    parameterization=parameterization,
                )
            )
            self.norms.append(nn.LayerNorm(rec1_size))
            self.dropouts.append(nn.Dropout1d(dropout))

    def forward(self, x):
        """Input x is shape (B, L, d_model)"""

        x = x.transpose(-1, -2)  # (B, L, d_model) -> (B, d_model, L)
        for layer, norm, dropout in zip(self.stablessm_layers, self.norms, self.dropouts):
            # Each iteration of this loop will map (B, d_model, L) -> (B, d_model, L)

            z = x
            if self.prenorm:
                # Prenorm
                z = norm(z.transpose(-1, -2)).transpose(-1, -2)

            # Apply StableSSM block: we ignore the state input and output
            z, _ = layer(z)

            # Dropout on the output of the StableSSM block
            z = dropout(z)

            # Residual connection
            x = z + x

            if not self.prenorm:
                # Postnorm
                x = norm(x.transpose(-1, -2)).transpose(-1, -2)

        x = x.transpose(-1, -2)  # (B, d_model, L) -> (B, L, d_model)

        # Pooling: average pooling over the sequence length
        if not self.return_seq:
            x = x.mean(dim=1)  # This is actually a linear convolution layer...
        else:
            x = x

        return x
