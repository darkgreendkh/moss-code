"""Provider-neutral model request objects.

The control loop owns semantic prompt sections; provider clients own their HTTP
wire format.  Keeping that boundary explicit lets role separation and cache
breakpoints evolve without making the loop speak provider-specific JSON.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Block:
    text: str
    kind: str
    source: str = ""
    trust: str = "platform"
    cache: bool = False


@dataclass(frozen=True)
class Message:
    role: str
    blocks: tuple[Block, ...]
    call_id: str | None = None


@dataclass(frozen=True)
class ModelRequest:
    system: tuple[Block, ...] = ()
    messages: tuple[Message, ...] = ()
    tools: tuple[dict, ...] = ()
    max_new_tokens: int = 4096
    cache_key: str | None = None
    protocol: str = "text"

    def flatten(self):
        """拍平成旧的单字符串 prompt，供兼容 client 与逐字节回归使用。"""
        blocks = [*self.system]
        for message in self.messages:
            blocks.extend(message.blocks)
        return "\n\n".join(block.text for block in blocks if block.text).strip()


@dataclass(frozen=True)
class PromptBundle:
    request: ModelRequest
    text: str
    metadata: dict = field(default_factory=dict)
