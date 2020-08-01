# Copyright urw7rs
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn as nn
import torch.nn.functional as F

import nest


class Net(nn.Module):
    def __init__(self, obs_shape, action_shape, grid_shape):
        super(Net, self).__init__()
        self._num_actions = len(action_shape)

        c, h, w = obs_shape
        assert h == 64 and w == 64

        self.obs = nn.Conv2d(c + 2, 32, 5, 1, 2)

        self.mask_mlp = MaskMLP(action_shape, grid_shape)
        self.action = nn.Sequential(
            nn.Linear(16 * self._num_actions, 64),
            nn.ReLU(inplace=True),
            nn.Linear(64, 32),
            nn.ReLU(inplace=True),
            nn.Linear(32, 32),
            nn.ReLU(inplace=True),
        )

        self.noise = nn.Sequential(
            nn.Linear(10, 64),
            nn.ReLU(inplace=True),
            nn.Linear(64, 32),
            nn.ReLU(inplace=True),
            nn.Linear(32, 32),
            nn.ReLU(inplace=True),
        )

        self.relu = nn.ReLU(inplace=True)

        self.base = nn.Sequential(
            # conv
            nn.Conv2d(32, 32, 4, 2, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 32, 4, 2, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(32, 32, 4, 2, 1),
            nn.ReLU(inplace=True),
            # resblock
            *[ResBlock(32) for _ in range(8)],
            # flatten_fc
            nn.Flatten(1, 3),
            nn.Linear(8 * 8 * 32, 256),
            # relu
            nn.ReLU(inplace=True),
        )

        self.lstm = nn.LSTM(256, 256, num_layers=1)

        self.policy = Decoder(action_shape, grid_shape)
        self.baseline = nn.Linear(256, 1)

    def _grid(self, batch, h, w):
        y_grid = torch.linspace(-1, 1, h)
        y_grid = y_grid.view(1, 1, h, 1)
        y_grid = y_grid.repeat(batch, 1, 1, h)

        x_grid = torch.linspace(-1, 1, w)
        x_grid = x_grid.view(1, 1, 1, w)
        x_grid = x_grid.repeat(batch, 1, w, 1)

        return torch.cat([y_grid, x_grid], dim=1)

    def initial_state(self, batch_size=1):
        return tuple(torch.ones(1, batch_size, 256) for _ in range(2))

    def forward(self, obs, done, core_state):
        T, B, C, H, W = obs["canvas"].shape
        grid = self._grid(T * B, H, W)

        notdone = (~done).float()
        obs["prev_action"] = obs["prev_action"] * notdone.unsqueeze(dim=2)

        obs = nest.map(lambda t: torch.flatten(t, 0, 1), obs)

        canvas, action_mask, action, noise = (
            obs[k] for k in ["canvas", "action_mask", "prev_action", "noise_sample"]
        )

        features = self.obs(torch.cat([canvas, grid], dim=1))

        condition = (
            self.noise(noise) + self.action(self.mask_mlp(action, action_mask))
        ).view(-1, 32, 1, 1)

        embedding = self.base(self.relu(features + condition)).view(T, B, 256)

        core_output_list = []
        for core_input, nd in zip(embedding.unbind(), notdone.unbind()):
            nd = nd.view(1, -1, 1)
            core_state = nest.map(nd.mul, core_state)
            output, core_state = self.lstm(core_input.unsqueeze(0), core_state)
            core_output_list.append(output)
        seed = torch.flatten(torch.cat(core_output_list), 0, 1)

        action, logits = self.policy(seed, action)
        baseline = self.baseline(seed)

        action = action.view(T, B, self._num_actions)
        baseline = baseline.view(T, B)
        logits = nest.map(lambda t: t.view(T, B, -1), logits)

        return (action, logits, baseline), core_state


class Decoder(nn.Module):
    SPATIAL_ACTIONS = ["end", "control"]

    def __init__(self, action_shape, grid_shape):
        super(Decoder, self).__init__()
        self.num_actions = len(action_shape)
        modules = []
        action_shape = action_shape
        for i in range(self.num_actions):
            if i < 2:
                module = nn.Sequential(
                    View(-1, 16, 4, 4),
                    nn.ConvTranspose2d(16, 32, 4, 2, 1),
                    *[ResBlock(32) for i in range(8)],
                    nn.ConvTranspose2d(32, 32, 4, 2, 1),
                    nn.ConvTranspose2d(32, 32, 4, 2, 1),
                    nn.Conv2d(32, 1, 3, 1, 1),
                    View(-1, 32 * 32),
                )
            else:
                module = nn.Linear(256, action_shape[i])
            modules.append(module)
        self.decode = nn.ModuleList(modules)

        modules = []
        for i, shape in enumerate(action_shape[:-1]):
            if i < 2:
                module = Location(grid_shape)
            else:
                module = Scalar(shape)
            modules.append(module)

        self.mlp = nn.ModuleList(modules)

        self.concat_fc = nn.Sequential(nn.Linear(16 + 256, 256), nn.ReLU(inplace=True))
        self.relu = nn.ReLU(inplace=True)

    def forward(self, h, actions=None):
        logits = []
        if self.training:
            for i in range(self.num_actions):
                logit = self.decode[i](h)

                if i != self.num_actions - 1:
                    concat = torch.cat(
                        [h, self.mlp[i](actions[:, i : i + 1].float())], dim=1
                    )
                    residual = self.concat_fc(concat)
                    h = self.relu(h + residual)

                logits.append(logit)

            return actions, logits
        else:
            actions = []
            for i in range(self.num_actions):
                logit = self.decode[i](h)
                action = torch.multinomial(F.softmax(logit, dim=1), num_samples=1)

                if i != self.num_actions - 1:
                    concat = torch.cat([h, self.mlp[i](action.float())], dim=1)
                    residual = self.concat_fc(concat)
                    h = self.relu(h + residual)

                actions.append(action)
                logits.append(logit)

            actions = torch.cat(actions, dim=1)
            return actions, logits


class ConvDecoder(nn.Module):
    def __init__(self):
        super(ConvDecoder, self).__init__()
        self.conv = nn.Sequential(
            nn.ConvTranspose2d(16, 32, 4, 2, 1),
            *[ResBlock(32) for i in range(8)],
            nn.ConvTranspose2d(32, 32, 4, 2, 1),
            nn.ConvTranspose2d(32, 32, 4, 2, 1),
            nn.Conv2d(32, 1, 3, 1, 1),
        )

    def forward(self, z):
        output = self.conv(z.view(-1, 16, 4, 4))
        return output.view(-1, 32 * 32)


class View(nn.Module):
    def __init__(self, *args):
        super(View, self).__init__()
        self.size = args

    def forward(self, x):
        return x.view(*self.size)


class MaskMLP(nn.Module):
    def __init__(self, action_shape, grid_shape):
        super(MaskMLP, self).__init__()
        self.w, self.h = grid_shape
        modules = []
        for i, shape in enumerate(action_shape):
            if i < 2:
                module = Location(grid_shape)
            else:
                module = Scalar(shape)
            modules.append(module)

        self.mlp_list = nn.ModuleList(modules)

    def forward(self, action, action_mask):
        actions = action.unbind(dim=1)
        y = [
            self.mlp_list[i](action.unsqueeze(dim=1))
            for i, action in enumerate(actions)
        ]
        return torch.flatten(torch.stack(y, dim=1) * action_mask.unsqueeze(2), 1)


class Location(nn.Module):
    def __init__(self, shape):
        super(Location, self).__init__()
        self.w, self.h = shape
        self.linear = nn.Linear(2, 16)

    def forward(self, action):
        remainder = torch.fmod(action, self.w)
        x = -1.0 + 2.0 * (action - remainder) / (self.w - 1.0)
        y = -1.0 + 2.0 * remainder / (self.h - 1.0)
        action = torch.stack([y, x]).view(-1, 2)
        return self.linear(action)


class Scalar(nn.Module):
    def __init__(self, shape):
        super(Scalar, self).__init__()
        self.linear = nn.Linear(shape, 16)
        self._shape = shape

    def forward(self, action):
        action = F.one_hot(action.squeeze(dim=1).long(), self._shape).float()
        return self.linear(action)


class Linear(nn.Module):
    def __init__(self, *features):
        super(Linear, self).__init__()
        modules = []
        in_features = features[0]
        for out_features in features[1:]:
            modules.extend(
                [nn.Linear(in_features, out_features), nn.ReLU(inplace=True)]
            )
            in_features = out_features
        self.linear = nn.Sequential(*modules)

    def forward(self, x):
        return self.linear(x)


class ResBlock(nn.Module):
    def __init__(self, channels):
        super(ResBlock, self).__init__()
        self.conv = nn.Sequential(
            nn.Conv2d(channels, channels, 3, padding=1),
            nn.ReLU(inplace=True),
            nn.Conv2d(channels, channels, 3, padding=1),
        )
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x):
        residual = self.conv(x)
        residual += x
        return self.relu(residual)


class Discriminator(nn.Module):
    def __init__(self, obs_shape):
        super(Discriminator, self).__init__()
        c, h, w = obs_shape

        ndf = 64
        self.main = nn.Sequential(
            # (c) x 64 x 64
            nn.Conv2d(c, ndf, 4, 2, 1, bias=False),
            nn.LeakyReLU(0.2, True),
            # (ndf) x 32 x 32
            nn.Conv2d(ndf, ndf * 2, 4, 2, 1, bias=False),
            nn.LeakyReLU(0.2, True),
            # (ndf*2) x 16 x 16
            nn.Conv2d(ndf * 2, ndf * 4, 4, 2, 1, bias=False),
            nn.LeakyReLU(0.2, True),
            # (ndf*4) x 8 x 8
            nn.Conv2d(ndf * 4, ndf * 8, 4, 2, 1, bias=False),
            nn.LeakyReLU(0.2, True),
            # (ndf*8) x 4 x 4
            nn.Conv2d(ndf * 8, 1, 4, 1, 0, bias=False),
            nn.Flatten(0),
        )

        for module in self.main.modules():
            if isinstance(module, nn.Conv2d) or isinstance(module, nn.BatchNorm2d):
                nn.utils.spectral_norm(module)

    def forward(self, obs):
        x = self.main(obs)
        if self.training:
            return x
        else:
            return torch.sigmoid(x)


class ComplementDiscriminator(Discriminator, nn.Module):
    def __init__(self, obs_shape, power_iters):
        super(ComplementDiscriminator, self).__init__(obs_shape, power_iters)
        self.obs_shape = obs_shape

    def _mask(self):
        c, h, w = self.obs_shape
        left = torch.ones(1, c // 2, h, w // 2)
        right = torch.zeros(1, c // 2, h, w // 2)
        mask = torch.cat([left, right], dim=-1)
        mask = torch.cat([1 - mask, mask], dim=1)
        return mask

    def forward(self, obs):
        x = self.main(obs * self._mask)
        if self.training:
            return x
        else:
            return torch.sigmoid(x)
