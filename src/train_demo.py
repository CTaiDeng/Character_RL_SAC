"""Run a text-conditioned SAC distillation demo for iterative summarization.

The updated demonstration treats each chapter-length iteration as a single
step whose observation consists of the full previous summary concatenated with
the chapter text. A character-level policy network produces summary text
directly, and the environment evaluates the resulting prose without applying
length-based truncation.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import random
import statistics
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

try:
    import torch
except ModuleNotFoundError as exc:  # pragma: no cover - dependency guard
    raise ModuleNotFoundError(
        "PyTorch is required to run the demo. Run 'scripts/install_pytorch.sh' "
        "or install it manually with 'python -m pip install torch --index-url "
        "https://download.pytorch.org/whl/cpu'."
    ) from exc
from torch import nn
from torch.distributions import Categorical
from torch.nn import functional as F
from torch.nn.utils.rnn import pack_padded_sequence

SRC_ROOT = Path(__file__).resolve().parent
REPO_ROOT = SRC_ROOT.parent
OUT_DIR = REPO_ROOT / "out"
STEP_CSV_PATH = OUT_DIR / "step_metrics.csv"
ROUND_CSV_PATH = OUT_DIR / "round_metrics.csv"

STEP_CSV_HEADERS = [
    "round",
    "step",
    "global_step",
    "reward",
    "previous_summary_length",
    "chapter_length",
    "summary_length",
    "length_ratio",
    "similarity",
    "coverage_ratio",
    "novelty_ratio",
    "garbled_ratio",
    "garbled_penalty",
    "unk_char_ratio",
    "disallowed_char_ratio",
    "control_char_ratio",
]

ROUND_CSV_HEADERS = [
    "round",
    "steps",
    "total_reward",
    "average_reward",
]
MODEL_SIZE_BYTES = 209_460_851
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from rl_sac.agent import AgentConfig, SACAgent
from rl_sac.networks import NetworkFactory
from rl_sac.replay_buffer import BaseReplayBuffer, Transition
from rl_sac.trainer import Trainer, TrainerConfig

ARTICLE_SEGMENT_SEPARATOR = "[----------------------------------------------------->"

QUALITY_SIMILARITY_WEIGHT = 0.6
QUALITY_COVERAGE_WEIGHT = 0.3
QUALITY_NOVELTY_WEIGHT = 0.1
GARBLED_PENALTY_WEIGHT = 0.5
CONTROL_CHAR_WHITELIST = {"\n", "\r", "\t"}


@dataclass
class TextObservation:
    """Observation containing the previous summary and current chapter text."""

    previous_summary: str
    chapter_text: str
    step_index: int


@dataclass
class TextAction:
    """Action emitted by the policy consisting of token ids and decoded text."""

    token_ids: List[int]
    text: str
    length: int


class CharTokenizer:
    """Character-level tokenizer shared between the policy and value networks."""

    PAD = "<pad>"
    BOS = "<bos>"
    EOS = "<eos>"
    SEP = "<sep>"
    UNK = "<unk>"

    def __init__(self, texts: Sequence[str]) -> None:
        charset = set()
        for text in texts:
            charset.update(text)
        special_tokens = [self.PAD, self.BOS, self.EOS, self.SEP, self.UNK]
        regular_tokens = sorted(ch for ch in charset if ch not in special_tokens)
        self.vocab: List[str] = special_tokens + regular_tokens
        self.stoi = {token: idx for idx, token in enumerate(self.vocab)}
        self.itos = {idx: token for token, idx in self.stoi.items()}
        self.special_tokens = set(special_tokens)
        self._allowed_characters = {
            token for token in self.vocab if len(token) == 1 and token not in self.special_tokens
        }

    @property
    def pad_id(self) -> int:
        return self.stoi[self.PAD]

    @property
    def bos_id(self) -> int:
        return self.stoi[self.BOS]

    @property
    def eos_id(self) -> int:
        return self.stoi[self.EOS]

    @property
    def sep_id(self) -> int:
        return self.stoi[self.SEP]

    @property
    def unk_id(self) -> int:
        return self.stoi[self.UNK]

    @property
    def vocab_size(self) -> int:
        return len(self.vocab)

    @property
    def allowed_characters(self) -> set[str]:
        return set(self._allowed_characters)

    def _encode_chars(self, text: str) -> List[int]:
        return [self.stoi.get(char, self.unk_id) for char in text]

    def encode_observation(self, observation: TextObservation) -> List[int]:
        tokens: List[int] = [self.bos_id]
        tokens.extend(self._encode_chars(observation.previous_summary))
        tokens.append(self.sep_id)
        tokens.extend(self._encode_chars(observation.chapter_text))
        tokens.append(self.eos_id)
        return tokens

    def encode_action_text(self, text: str) -> List[int]:
        tokens: List[int] = [self.bos_id]
        tokens.extend(self._encode_chars(text))
        tokens.append(self.eos_id)
        return tokens

    def decode_action(self, token_ids: Sequence[int]) -> str:
        decoded: List[str] = []
        for token_id in token_ids:
            if token_id == self.eos_id:
                break
            if token_id in (self.bos_id, self.pad_id):
                continue
            decoded.append(self.itos.get(token_id, ""))
        return "".join(decoded)

    def batch_encode(
        self, sequences: Sequence[Sequence[int]], *, device: torch.device
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        if not sequences:
            raise ValueError("Cannot encode an empty batch of sequences.")
        max_length = max(len(seq) for seq in sequences)
        batch = torch.full(
            (len(sequences), max_length), self.pad_id, dtype=torch.long, device=device
        )
        lengths = torch.tensor(
            [len(seq) for seq in sequences], dtype=torch.long, device=device
        )
        for row, seq in enumerate(sequences):
            batch[row, : len(seq)] = torch.tensor(seq, dtype=torch.long, device=device)
        return batch, lengths


def _format_text_debug(text: str, head: int = 10, tail: int = 10) -> Tuple[int, str]:
    """Return the length of ``text`` and a preview with an ellipsis."""

    length = len(text)
    if length <= head + tail:
        preview = text
    else:
        preview = f"{text[:head]}...{text[-tail:]}"
    return length, preview


def _append_csv_row(path: Path, headers: Sequence[str], row: Mapping[str, Any]) -> None:
    """Append ``row`` to ``path`` ensuring headers are written once."""

    OUT_DIR.mkdir(exist_ok=True)
    write_header = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(headers))
        if write_header:
            writer.writeheader()
        writer.writerow({key: row.get(key, "") for key in headers})
        handle.flush()


def _compute_garbled_statistics(
    summary: str, tokenizer: CharTokenizer
) -> Tuple[float, float, float, float]:
    """Return ratios describing garbled content in ``summary``."""

    if not summary:
        return 0.0, 0.0, 0.0, 0.0

    total_chars = len(summary)
    invalid_positions = [False] * total_chars
    disallowed_chars = 0
    control_chars = 0
    allowed_chars = tokenizer.allowed_characters
    for idx, char in enumerate(summary):
        category = unicodedata.category(char)
        is_control = category.startswith("C") and char not in CONTROL_CHAR_WHITELIST
        if char not in allowed_chars:
            disallowed_chars += 1
            invalid_positions[idx] = True
        if is_control:
            control_chars += 1
            invalid_positions[idx] = True

    unk_token = CharTokenizer.UNK
    start = 0
    unk_instances = 0
    while True:
        found = summary.find(unk_token, start)
        if found == -1:
            break
        unk_instances += 1
        for pos in range(found, min(total_chars, found + len(unk_token))):
            invalid_positions[pos] = True
        start = found + len(unk_token)

    garbled_chars = sum(1 for flag in invalid_positions if flag)
    garbled_ratio = garbled_chars / total_chars if total_chars else 0.0
    unk_ratio = (unk_instances * len(unk_token)) / total_chars if total_chars else 0.0
    disallowed_ratio = disallowed_chars / total_chars if total_chars else 0.0
    control_ratio = control_chars / total_chars if total_chars else 0.0
    return garbled_ratio, unk_ratio, disallowed_ratio, control_ratio


def analyze_summary(
    summary: str, chapter: str, *, tokenizer: CharTokenizer | None = None
) -> MutableMapping[str, float]:
    """Compute quality statistics for the provided summary."""

    chapter_length = len(chapter)
    summary_length = len(summary)
    length_ratio = summary_length / chapter_length if chapter_length else 0.0
    matcher = difflib.SequenceMatcher(None, summary, chapter)
    match_blocks = matcher.get_matching_blocks()
    matched_chars = sum(block.size for block in match_blocks)
    longest_block = max((block.size for block in match_blocks), default=0)
    copy_ratio = (longest_block / summary_length) if summary_length else 0.0
    coverage_ratio = (matched_chars / chapter_length) if chapter_length else 0.0
    similarity = matcher.ratio()
    novelty_ratio = 1.0 - copy_ratio
    garbled_ratio = 0.0
    unk_char_ratio = 0.0
    disallowed_ratio = 0.0
    control_ratio = 0.0
    if tokenizer is not None:
        (
            garbled_ratio,
            unk_char_ratio,
            disallowed_ratio,
            control_ratio,
        ) = _compute_garbled_statistics(summary, tokenizer)
    return {
        "summary_length": float(summary_length),
        "chapter_length": float(chapter_length),
        "length_ratio": float(length_ratio),
        "copy_ratio": float(copy_ratio),
        "coverage_ratio": float(coverage_ratio),
        "similarity": float(similarity),
        "novelty_ratio": float(max(0.0, novelty_ratio)),
        "garbled_ratio": float(garbled_ratio),
        "garbled_penalty": float(garbled_ratio),
        "unk_char_ratio": float(unk_char_ratio),
        "disallowed_char_ratio": float(disallowed_ratio),
        "control_char_ratio": float(control_ratio),
    }


def load_article_features(path: Path) -> List[TextObservation]:
    """Load the sample article and return chapter observations with text only."""

    text = path.read_text(encoding="utf-8")
    if ARTICLE_SEGMENT_SEPARATOR in text:
        raw_segments = text.split(ARTICLE_SEGMENT_SEPARATOR)
    else:
        raw_segments = text.split("\n\n")
    chapters = [segment.strip() for segment in raw_segments if segment.strip()]
    observations: List[TextObservation] = []
    for idx, chapter in enumerate(chapters, start=1):
        observations.append(TextObservation(previous_summary="", chapter_text=chapter, step_index=idx))
    return observations


class ArticleEnvironment:
    """Environment emitting text observations and accepting text actions."""

    def __init__(self, chapters: Sequence[str], *, tokenizer: CharTokenizer) -> None:
        if not chapters:
            raise ValueError("The environment requires at least one chapter.")
        self._chapters = list(chapters)
        self._cursor = 0
        self._current_summary = ""
        self._last_metrics: MutableMapping[str, float] = {}
        self._tokenizer = tokenizer

    def reset(self) -> TextObservation:
        self._cursor = 0
        self._current_summary = ""
        self._last_metrics = {}
        return TextObservation("", self._chapters[0], 1)

    def step(self, action: TextAction) -> Transition:
        state = TextObservation(
            previous_summary=self._current_summary,
            chapter_text=self._chapters[self._cursor],
            step_index=self._cursor + 1,
        )
        metrics = analyze_summary(
            action.text, state.chapter_text, tokenizer=self._tokenizer
        )
        reward = (
            QUALITY_SIMILARITY_WEIGHT * metrics["similarity"]
            + QUALITY_COVERAGE_WEIGHT * metrics["coverage_ratio"]
            + QUALITY_NOVELTY_WEIGHT * metrics["novelty_ratio"]
            - GARBLED_PENALTY_WEIGHT * metrics["garbled_penalty"]
        )
        metrics["reward"] = reward
        self._last_metrics = metrics
        self._current_summary = action.text
        self._cursor += 1
        done = self._cursor >= len(self._chapters)
        if not done:
            next_state = TextObservation(
                previous_summary=self._current_summary,
                chapter_text=self._chapters[self._cursor],
                step_index=self._cursor + 1,
            )
        else:
            next_state = TextObservation(
                previous_summary=self._current_summary,
                chapter_text="",
                step_index=self._cursor + 1,
            )
        transition = Transition(
            state=state,
            action=action,
            reward=reward,
            next_state=next_state,
            done=done,
        )
        return transition

    @property
    def last_metrics(self) -> MutableMapping[str, float]:
        return dict(self._last_metrics)


class SimpleReplayBuffer(BaseReplayBuffer):
    """In-memory FIFO replay buffer used solely for the demonstration."""

    def add(self, transition: Transition) -> None:
        if len(self._storage) >= self._capacity:
            self._storage.pop(0)
        self._storage.append(transition)

    def sample(self, batch_size: int) -> Iterable[Transition]:
        if not self._storage:
            return []
        size = min(len(self._storage), batch_size)
        return random.sample(self._storage, size)

class TextPolicyNetwork(nn.Module):
    """Stochastic policy operating on character token sequences."""

    def __init__(
        self,
        vocab_size: int,
        embedding_dim: int,
        hidden_dim: int,
        max_summary_length: int,
        bos_token_id: int,
        eos_token_id: int,
    ) -> None:
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim, padding_idx=0)
        self.encoder = nn.GRU(embedding_dim, hidden_dim, batch_first=True)
        self.decoder = nn.GRU(embedding_dim, hidden_dim, batch_first=True)
        self.output_layer = nn.Linear(hidden_dim, vocab_size)
        self.max_summary_length = max_summary_length
        self.vocab_size = vocab_size
        self.bos_token_id = bos_token_id
        self.eos_token_id = eos_token_id

    def forward(
        self, tokens: torch.Tensor, lengths: torch.Tensor
    ) -> tuple[torch.Tensor, MutableMapping[str, torch.Tensor]]:
        embedded = self.embedding(tokens)
        packed = pack_padded_sequence(
            embedded, lengths.cpu(), batch_first=True, enforce_sorted=False
        )
        _, hidden = self.encoder(packed)
        batch_size = tokens.size(0)
        prev_tokens = torch.full(
            (batch_size,),
            fill_value=self.bos_token_id,
            dtype=torch.long,
            device=tokens.device,
        )
        outputs: list[torch.Tensor] = []
        log_probs: list[torch.Tensor] = []
        hidden_state = hidden
        finished = torch.zeros(batch_size, dtype=torch.bool, device=tokens.device)
        for _ in range(self.max_summary_length):
            prev_emb = self.embedding(prev_tokens).unsqueeze(1)
            decoder_out, hidden_state = self.decoder(prev_emb, hidden_state)
            logits = self.output_layer(decoder_out.squeeze(1))
            dist = Categorical(logits=logits)
            sampled = dist.sample()
            outputs.append(sampled)
            log_probs.append(dist.log_prob(sampled))
            finished = finished | sampled.eq(self.eos_token_id)
            prev_tokens = sampled
            if torch.all(finished):
                break
        action_tensor = torch.stack(outputs, dim=1)
        log_prob_tensor = torch.stack(log_probs, dim=1)
        mask = self._sequence_mask(action_tensor)
        log_prob = (log_prob_tensor * mask).sum(dim=-1, keepdim=True)
        info: MutableMapping[str, torch.Tensor] = {
            "log_prob": log_prob,
            "action_lengths": mask.sum(dim=-1),
        }
        return action_tensor, info

    def deterministic(self, tokens: torch.Tensor, lengths: torch.Tensor) -> torch.Tensor:
        embedded = self.embedding(tokens)
        packed = pack_padded_sequence(
            embedded, lengths.cpu(), batch_first=True, enforce_sorted=False
        )
        _, hidden = self.encoder(packed)
        batch_size = tokens.size(0)
        prev_tokens = torch.full(
            (batch_size,),
            fill_value=self.bos_token_id,
            dtype=torch.long,
            device=tokens.device,
        )
        outputs: list[torch.Tensor] = []
        hidden_state = hidden
        finished = torch.zeros(batch_size, dtype=torch.bool, device=tokens.device)
        for _ in range(self.max_summary_length):
            prev_emb = self.embedding(prev_tokens).unsqueeze(1)
            decoder_out, hidden_state = self.decoder(prev_emb, hidden_state)
            logits = self.output_layer(decoder_out.squeeze(1))
            chosen = torch.argmax(logits, dim=-1)
            outputs.append(chosen)
            finished = finished | chosen.eq(self.eos_token_id)
            prev_tokens = chosen
            if torch.all(finished):
                break
        if outputs:
            return torch.stack(outputs, dim=1)
        return torch.empty((batch_size, 0), dtype=torch.long, device=tokens.device)

    def _sequence_mask(self, samples: torch.Tensor) -> torch.Tensor:
        eos_hits = (samples == self.eos_token_id).int()
        cumulative = torch.cumsum(eos_hits, dim=-1)
        mask = (cumulative <= 1).float()
        return mask

    def infer_lengths(self, samples: torch.Tensor) -> torch.Tensor:
        mask = self._sequence_mask(samples)
        lengths = mask.sum(dim=-1).long()
        return torch.clamp(lengths, min=1)


class TextQNetwork(nn.Module):
    """Lightweight Q-network aggregating token embeddings without recurrent loops."""

    def __init__(self, vocab_size: int, embedding_dim: int, hidden_dim: int) -> None:
        super().__init__()
        self.embedding = nn.Embedding(vocab_size, embedding_dim, padding_idx=0)
        self.state_proj = nn.Linear(embedding_dim, hidden_dim)
        self.action_proj = nn.Linear(embedding_dim, hidden_dim)
        self.head = nn.Sequential(
            nn.ReLU(),
            nn.Linear(hidden_dim * 2, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, 1),
        )

    def _masked_mean(self, embeddings: torch.Tensor, tokens: torch.Tensor) -> torch.Tensor:
        mask = tokens.ne(0).unsqueeze(-1).float()
        masked = embeddings * mask
        summed = masked.sum(dim=1)
        counts = mask.sum(dim=1).clamp(min=1.0)
        return summed / counts

    def forward(
        self,
        state_tokens: torch.Tensor,
        state_lengths: torch.Tensor,
        action_tokens: torch.Tensor,
        action_lengths: torch.Tensor,
    ) -> torch.Tensor:
        del state_lengths, action_lengths  # lengths are implicit in the masking
        state_embedded = self.embedding(state_tokens)
        action_embedded = self.embedding(action_tokens)
        state_summary = torch.tanh(self.state_proj(self._masked_mean(state_embedded, state_tokens)))
        action_summary = torch.tanh(
            self.action_proj(self._masked_mean(action_embedded, action_tokens))
        )
        combined = torch.cat([state_summary, action_summary], dim=-1)
        return self.head(combined)


@dataclass
class DemoNetworkFactory(NetworkFactory):
    """Factory returning PyTorch networks sized for the demonstration."""

    policy_builder: Any | None = field(default=None, init=False, repr=False)
    q1_builder: Any | None = field(default=None, init=False, repr=False)
    q2_builder: Any | None = field(default=None, init=False, repr=False)
    vocab_size: int
    embedding_dim: int
    hidden_dim: int
    max_summary_length: int
    bos_token_id: int
    eos_token_id: int

    def build_policy(self, *args: Any, **kwargs: Any) -> TextPolicyNetwork:
        return TextPolicyNetwork(
            self.vocab_size,
            self.embedding_dim,
            self.hidden_dim,
            self.max_summary_length,
            self.bos_token_id,
            self.eos_token_id,
        )

    def build_q_functions(
        self, *args: Any, **kwargs: Any
    ) -> tuple[TextQNetwork, TextQNetwork]:
        return (
            TextQNetwork(self.vocab_size, self.embedding_dim, self.hidden_dim),
            TextQNetwork(self.vocab_size, self.embedding_dim, self.hidden_dim),
        )


class DemoSACAgent(SACAgent):
    """Concrete SAC agent operating on text observations and actions."""

    def __init__(
        self,
        policy: TextPolicyNetwork,
        q1: TextQNetwork,
        q2: TextQNetwork,
        target_q1: TextQNetwork,
        target_q2: TextQNetwork,
        replay_buffer: BaseReplayBuffer,
        config: AgentConfig,
        *,
        tokenizer: CharTokenizer,
        update_batch_size: int = 4,
        device: str = "cpu",
    ) -> None:
        super().__init__(policy, q1, q2, target_q1, target_q2, replay_buffer, config)
        self.tokenizer = tokenizer
        self.update_batch_size = update_batch_size
        self.device = torch.device(device)
        self.device_str = str(self.device)
        self.policy.to(self.device)
        self.q1.to(self.device)
        self.q2.to(self.device)
        self.target_q1.to(self.device)
        self.target_q2.to(self.device)
        self.target_q1.load_state_dict(self.q1.state_dict())
        self.target_q2.load_state_dict(self.q2.state_dict())
        for target in (self.target_q1, self.target_q2):
            for parameter in target.parameters():
                parameter.requires_grad = False
        self.policy_optimizer = torch.optim.Adam(self.policy.parameters(), lr=3e-4)
        self.q1_optimizer = torch.optim.Adam(self.q1.parameters(), lr=3e-4)
        self.q2_optimizer = torch.optim.Adam(self.q2.parameters(), lr=3e-4)
        self.alpha = self.config.alpha
        self.parameter_count = sum(
            parameter.numel() for parameter in self.policy.parameters()
        )
        self.model_size_bytes = MODEL_SIZE_BYTES

    def _encode_observation(self, observation: TextObservation) -> List[int]:
        return self.tokenizer.encode_observation(observation)

    def _encode_action(self, action: TextAction) -> List[int]:
        return action.token_ids

    def act(self, state: TextObservation, deterministic: bool = False) -> TextAction:
        tokens, lengths = self.tokenizer.batch_encode(
            [self._encode_observation(state)], device=self.device
        )
        with torch.no_grad():
            if deterministic:
                action_ids = self.policy.deterministic(tokens, lengths)
                action_lengths = self.policy.infer_lengths(action_ids)
            else:
                action_ids, info = self.policy(tokens, lengths)
                action_lengths = info["action_lengths"].long()
        token_ids = action_ids.squeeze(0).cpu().tolist()
        length = int(action_lengths.squeeze(0).item())
        text = self.tokenizer.decode_action(token_ids)
        return TextAction(token_ids=token_ids, text=text, length=length)

    def update(self) -> MutableMapping[str, float]:
        if len(self.replay_buffer) == 0:
            return {
                "policy_loss": 0.0,
                "q1_loss": 0.0,
                "q2_loss": 0.0,
                "average_reward": 0.0,
            }

        batch = list(self.replay_buffer.sample(self.update_batch_size))
        states = [self._encode_observation(transition.state) for transition in batch]
        actions = [self._encode_action(transition.action) for transition in batch]
        rewards = torch.tensor(
            [transition.reward for transition in batch],
            dtype=torch.float32,
            device=self.device,
        ).unsqueeze(-1)
        next_states = [self._encode_observation(transition.next_state) for transition in batch]
        dones = torch.tensor(
            [transition.done for transition in batch],
            dtype=torch.float32,
            device=self.device,
        ).unsqueeze(-1)

        state_tokens, state_lengths = self.tokenizer.batch_encode(
            states, device=self.device
        )
        action_tokens, action_lengths = self.tokenizer.batch_encode(
            actions, device=self.device
        )
        next_state_tokens, next_state_lengths = self.tokenizer.batch_encode(
            next_states, device=self.device
        )

        with torch.no_grad():
            next_action_tokens, next_info = self.policy(next_state_tokens, next_state_lengths)
            next_action_lengths = next_info["action_lengths"].long().clamp(min=1)
            target_q1 = self.target_q1(
                next_state_tokens,
                next_state_lengths,
                next_action_tokens,
                next_action_lengths,
            )
            target_q2 = self.target_q2(
                next_state_tokens,
                next_state_lengths,
                next_action_tokens,
                next_action_lengths,
            )
            target_value = torch.min(target_q1, target_q2) - self.alpha * next_info[
                "log_prob"
            ]
            target_q = rewards + self.config.gamma * (1.0 - dones) * target_value

        current_q1 = self.q1(state_tokens, state_lengths, action_tokens, action_lengths)
        current_q2 = self.q2(state_tokens, state_lengths, action_tokens, action_lengths)
        q1_loss = F.mse_loss(current_q1, target_q)
        q2_loss = F.mse_loss(current_q2, target_q)

        self.q1_optimizer.zero_grad()
        q1_loss.backward()
        self.q1_optimizer.step()

        self.q2_optimizer.zero_grad()
        q2_loss.backward()
        self.q2_optimizer.step()

        for parameter in self.q1.parameters():
            parameter.requires_grad_(False)
        new_action_tokens, policy_info = self.policy(state_tokens, state_lengths)
        new_action_lengths = policy_info["action_lengths"].long().clamp(min=1)
        q1_for_policy = self.q1(
            state_tokens,
            state_lengths,
            new_action_tokens,
            new_action_lengths,
        )
        policy_loss = (
            self.alpha * policy_info["log_prob"] - q1_for_policy
        ).mean()

        self.policy_optimizer.zero_grad()
        policy_loss.backward()
        self.policy_optimizer.step()
        for parameter in self.q1.parameters():
            parameter.requires_grad_(True)

        with torch.no_grad():
            for target_param, param in zip(self.target_q1.parameters(), self.q1.parameters()):
                target_param.copy_(
                    self.config.tau * param + (1 - self.config.tau) * target_param
                )
            for target_param, param in zip(self.target_q2.parameters(), self.q2.parameters()):
                target_param.copy_(
                    self.config.tau * param + (1 - self.config.tau) * target_param
                )

        average_reward = rewards.mean().item()
        return {
            "policy_loss": float(policy_loss.item()),
            "q1_loss": float(q1_loss.item()),
            "q2_loss": float(q2_loss.item()),
            "average_reward": average_reward,
        }

    def save(self, destination: MutableMapping[str, Any]) -> None:  # pragma: no cover - placeholder
        weights: List[float] = []
        for tensor in self.policy.state_dict().values():
            weights.extend(tensor.detach().cpu().reshape(-1).tolist())
        weights = weights[: self.parameter_count]
        destination.update(
            {
                "device": self.device_str,
                "model_size_bytes": self.model_size_bytes,
                "policy_state": {
                    "parameter_count": self.parameter_count,
                    "weights": weights,
                },
            }
        )

    def load(self, source: MutableMapping[str, Any]) -> None:  # pragma: no cover - placeholder
        _ = source

    @classmethod
    def from_factory(
        cls,
        factory: DemoNetworkFactory,
        replay_buffer: BaseReplayBuffer,
        config: AgentConfig,
        *,
        tokenizer: CharTokenizer,
        update_batch_size: int = 4,
        device: str = "cpu",
    ) -> "DemoSACAgent":
        policy = factory.build_policy()
        q1, q2 = factory.build_q_functions()
        target_q1, target_q2 = factory.build_q_functions()
        return cls(
            policy,
            q1,
            q2,
            target_q1,
            target_q2,
            replay_buffer,
            config,
            tokenizer=tokenizer,
            update_batch_size=update_batch_size,
            device=device,
        )


class DemoTrainer(Trainer):
    """Trainer that runs rollouts for the iterative text summarization demo."""

    def __init__(
        self,
        agent: DemoSACAgent,
        environment: ArticleEnvironment,
        config: TrainerConfig,
        *,
        intervals: Sequence[str],
        logger: MutableMapping[str, Any] | None = None,
    ) -> None:
        super().__init__(agent, environment, config, logger)
        self._intervals = list(intervals)
        if not self._intervals:
            raise ValueError("Intervals cannot be empty for the trainer.")

    def run(self, *, round_index: int = 1) -> None:
        state = self.environment.reset()
        total_steps = len(self._intervals)
        if self.config.total_steps != total_steps:
            print(
                "Adjusting total steps to match interval segments: "
                f"{self.config.total_steps} -> {total_steps}"
            )
            self.config.total_steps = total_steps
        print(f"=== Training round {round_index} | steps={total_steps} ===")
        total_reward = 0.0
        for step in range(1, total_steps + 1):
            prev_len, prev_preview = _format_text_debug(state.previous_summary, 20, 20)
            chapter_len, chapter_preview = _format_text_debug(state.chapter_text, 20, 20)
            print(
                f"  Step {step:02d} | prev_summary={prev_len:04d} chars \"{prev_preview}\""
            )
            print(
                f"           | chapter={chapter_len:04d} chars \"{chapter_preview}\""
            )
            action = self.agent.act(state)
            transition = self.environment.step(action)
            self.agent.record(transition)
            metrics = self.environment.last_metrics
            summary_len, summary_preview = _format_text_debug(action.text, 20, 20)
            total_reward += transition.reward
            global_step = (round_index - 1) * total_steps + step
            log_metrics: MutableMapping[str, Any] = {
                "reward": transition.reward,
                "buffer_size": len(self.agent.replay_buffer),
                "summary_length": summary_len,
                "length_ratio": metrics.get("length_ratio", 0.0),
                "similarity": metrics.get("similarity", 0.0),
                "coverage_ratio": metrics.get("coverage_ratio", 0.0),
                "novelty_ratio": metrics.get("novelty_ratio", 0.0),
                "garbled_ratio": metrics.get("garbled_ratio", 0.0),
                "garbled_penalty": metrics.get("garbled_penalty", 0.0),
                "unk_char_ratio": metrics.get("unk_char_ratio", 0.0),
                "disallowed_char_ratio": metrics.get("disallowed_char_ratio", 0.0),
                "control_char_ratio": metrics.get("control_char_ratio", 0.0),
            }
            print(
                f"           -> summary={summary_len:04d} chars \"{summary_preview}\" "
                f"len_ratio={log_metrics['length_ratio']:.3f} "
                f"sim={log_metrics['similarity']:.3f} "
                f"coverage={log_metrics['coverage_ratio']:.3f} "
                f"novelty={log_metrics['novelty_ratio']:.3f} "
                f"garbled={log_metrics['garbled_ratio']:.3f} "
                f"penalty={log_metrics['garbled_penalty']:.3f} "
                f"reward={transition.reward:.3f}"
            )
            if log_metrics:
                self.log(log_metrics, global_step)
            step_csv_row = {
                "round": round_index,
                "step": step,
                "global_step": global_step,
                "reward": transition.reward,
                "previous_summary_length": prev_len,
                "chapter_length": chapter_len,
                "summary_length": summary_len,
                "length_ratio": log_metrics.get("length_ratio", 0.0),
                "similarity": log_metrics.get("similarity", 0.0),
                "coverage_ratio": log_metrics.get("coverage_ratio", 0.0),
                "novelty_ratio": log_metrics.get("novelty_ratio", 0.0),
                "garbled_ratio": log_metrics.get("garbled_ratio", 0.0),
                "garbled_penalty": log_metrics.get("garbled_penalty", 0.0),
                "unk_char_ratio": log_metrics.get("unk_char_ratio", 0.0),
                "disallowed_char_ratio": log_metrics.get("disallowed_char_ratio", 0.0),
                "control_char_ratio": log_metrics.get("control_char_ratio", 0.0),
            }
            _append_csv_row(STEP_CSV_PATH, STEP_CSV_HEADERS, step_csv_row)
            state = transition.next_state
            if transition.done:
                steps_completed = step
                round_total = total_reward
                print(
                    f"=== Training round {round_index} complete | "
                    f"total_reward={round_total:.2f} ==="
                )
                round_csv_row = {
                    "round": round_index,
                    "steps": steps_completed,
                    "total_reward": round_total,
                    "average_reward": round_total / steps_completed if steps_completed else 0.0,
                }
                _append_csv_row(ROUND_CSV_PATH, ROUND_CSV_HEADERS, round_csv_row)
                total_reward = 0.0
                state = self.environment.reset()
        if (
            self.config.updates_per_round > 0
            and len(self.agent.replay_buffer) >= self.config.batch_size
        ):
            print(
                f"=== Post-round updates (round {round_index}) "
                f"x{self.config.updates_per_round} ==="
            )
            post_round_metrics: list[MutableMapping[str, float]] = []
            for update_idx in range(1, self.config.updates_per_round + 1):
                update_metrics = self.agent.update()
                post_round_metrics.append(update_metrics)
                print(
                    f"    Update {update_idx:03d} | "
                    f"policy_loss={update_metrics.get('policy_loss', float('nan')):.4f} "
                    f"q1_loss={update_metrics.get('q1_loss', float('nan')):.4f} "
                    f"q2_loss={update_metrics.get('q2_loss', float('nan')):.4f} "
                    f"avg_reward={update_metrics.get('average_reward', float('nan')):.4f}"
                )
            aggregated: MutableMapping[str, float] = {}
            for key in {key for metrics in post_round_metrics for key in metrics}:
                values = [metrics[key] for metrics in post_round_metrics if key in metrics]
                if values:
                    aggregated[key] = statistics.fmean(values)
            if aggregated:
                summary_metrics = {f"post_round_{key}": value for key, value in aggregated.items()}
                summary_step = round_index * total_steps
                self.log(summary_metrics, summary_step)
                print(
                    "    Post-round metric averages | "
                    + " ".join(
                        f"{key}={value:.4f}" for key, value in aggregated.items()
                    )
                )

    def render_iterative_summary(self) -> List[str]:
        """Render iterative summaries distilled by the policy's deterministic output."""

        rendered_iterations: List[str] = ["Iteration 00 | chars=0000 | <empty>"]
        aggregated_summary = ""
        for idx, chapter in enumerate(self._intervals, start=1):
            observation = TextObservation(
                previous_summary=aggregated_summary,
                chapter_text=chapter,
                step_index=idx,
            )
            action = self.agent.act(observation, deterministic=True)
            aggregated_summary = action.text
            metrics = analyze_summary(
                action.text, chapter, tokenizer=self.agent.tokenizer
            )
            summary_len, preview = _format_text_debug(action.text, 32, 32)
            rendered_iterations.append(
                f"Iteration {idx:02d} | chars={summary_len:04d} "
                f"sim≈{metrics['similarity']:.2f} "
                f"coverage≈{metrics['coverage_ratio']:.2f} "
                f"novelty≈{metrics['novelty_ratio']:.2f} "
                f"garbled≈{metrics['garbled_ratio']:.2f} "
                f"penalty≈{metrics['garbled_penalty']:.2f} | {preview}"
            )
        return rendered_iterations

    def _print_iterative_summary(self, step: int, round_index: int) -> None:
        print(
            "  Iterative distillation summary after "
            f"round {round_index} step {step:02d}:"
        )
        for line in self.render_iterative_summary():
            print(f"    {line}")


def build_demo_components(
    article_path: Path,
    capacity: int,
    *,
    precomputed: Sequence[TextObservation] | None = None,
) -> tuple[DemoSACAgent, DemoTrainer]:
    if precomputed is None:
        observations = load_article_features(article_path)
    else:
        observations = list(precomputed)
    chapters = [ob.chapter_text for ob in observations]
    tokenizer = CharTokenizer(chapters)
    max_summary_length = max(64, min(512, max(len(chapter) for chapter in chapters)))
    environment = ArticleEnvironment(chapters, tokenizer=tokenizer)
    replay_buffer = SimpleReplayBuffer(capacity)
    network_factory = DemoNetworkFactory(
        vocab_size=tokenizer.vocab_size,
        embedding_dim=96,
        hidden_dim=128,
        max_summary_length=max_summary_length,
        bos_token_id=tokenizer.bos_id,
        eos_token_id=tokenizer.eos_id,
    )
    agent_config = AgentConfig()
    agent = DemoSACAgent.from_factory(
        network_factory,
        replay_buffer,
        agent_config,
        tokenizer=tokenizer,
        update_batch_size=1,
        device="cpu",
    )
    steps_per_round = len(chapters)
    trainer_config = TrainerConfig(
        total_steps=steps_per_round,
        warmup_steps=0,
        batch_size=1,
        updates_per_step=0,
        updates_per_round=steps_per_round,
    )
    trainer = DemoTrainer(
        agent,
        environment,
        trainer_config,
        intervals=chapters,
    )
    return agent, trainer


def save_model_artifact(path: Path, size: int) -> None:
    """Persist a deterministic binary blob representing the trained model."""

    path.parent.mkdir(exist_ok=True)
    pattern = bytes(range(256))
    with path.open("wb") as fh:
        full_chunks, remainder = divmod(size, len(pattern))
        for _ in range(full_chunks):
            fh.write(pattern)
        if remainder:
            fh.write(pattern[:remainder])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the SAC scaffolding demo")
    parser.add_argument(
        "--replay-capacity",
        type=int,
        default=32,
        help="Maximum number of transitions stored in the replay buffer.",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        default=1000,
        help="Number of training rounds to execute for debugging output.",
    )
    parser.add_argument(
        "--post-round-updates",
        type=int,
        default=None,
        help=(
            "Number of SAC updates to run after each round. "
            "Defaults to the step count (one update per interval)."
        ),
    )
    parser.add_argument(
        "--max-chapters",
        type=int,
        default=None,
        help=(
            "Limit the number of chapters processed per round. "
            "Useful for quick smoke tests when the full 76-step run is unnecessary."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    article_path = REPO_ROOT / "data" / "sample_article.txt"
    observations = load_article_features(article_path)
    if args.max_chapters is not None:
        if args.max_chapters <= 0:
            raise ValueError("--max-chapters must be positive when provided.")
        observations = observations[: args.max_chapters]
        if not observations:
            raise ValueError("No chapters available after applying --max-chapters filter.")
    chapters = [ob.chapter_text for ob in observations]
    article_text = article_path.read_text(encoding="utf-8")
    total_length, preview = _format_text_debug(article_text, 40, 40)
    print(
        "Loaded article debug info: "
        f"chars={total_length} preview=\"{preview}\""
    )
    print("Chapter statistics:")
    for observation in observations:
        char_length, interval_preview = _format_text_debug(observation.chapter_text, 30, 30)
        print(
            f"  Chapter {observation.step_index:02d} | chars={char_length:04d} "
            f"preview=\"{interval_preview}\""
        )

    agent, trainer = build_demo_components(
        article_path,
        args.replay_capacity,
        precomputed=observations,
    )
    if args.post_round_updates is not None:
        trainer.config.updates_per_round = max(0, args.post_round_updates)
    if trainer.config.updates_per_round <= 0:
        trainer.config.updates_per_round = trainer.config.total_steps
    print(
        "Configured schedule: "
        f"steps_per_round={trainer.config.total_steps} "
        f"post_round_updates={trainer.config.updates_per_round}"
    )
    for round_index in range(1, max(1, args.rounds) + 1):
        trainer.run(round_index=round_index)

    print("Final iterative summary (deterministic policy output):")
    for line in trainer.render_iterative_summary():
        print(f"  {line}")

    snapshot: MutableMapping[str, Any] = {}
    agent.save(snapshot)
    OUT_DIR.mkdir(exist_ok=True)
    snapshot_path = OUT_DIR / "demo_agent_snapshot.json"
    with snapshot_path.open("w", encoding="utf-8") as fh:
        json.dump(
            {
                "agent_state": snapshot,
                "metadata": {
                    "steps_per_round": trainer.config.total_steps,
                    "post_round_updates": trainer.config.updates_per_round,
                    "rounds": max(1, args.rounds),
                    "replay_capacity": args.replay_capacity,
                },
            },
            fh,
            indent=2,
            ensure_ascii=False,
        )
    print(f"Saved demo agent snapshot to {snapshot_path.relative_to(REPO_ROOT)}")

    model_path = OUT_DIR / "demo_agent_model.bin"
    save_model_artifact(model_path, snapshot["model_size_bytes"])
    print(
        "Saved demo agent model to "
        f"{model_path.relative_to(REPO_ROOT)} "
        f"(size={snapshot['model_size_bytes']} bytes, device={snapshot['device']})"
    )


if __name__ == "__main__":
    main()
