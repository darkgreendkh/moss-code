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
    # 原生工具协议里，一条 assistant 消息可以带多个 tool_use，对应的 tool_result
    # 也必须挤在**同一条** user 消息里（Anthropic /messages 是硬校验）。所以调用 ID
    # 挂在 block 上而不是只挂在 message 上；message.call_id 保留给单块消息兜底。
    call_id: str = ""


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


@dataclass(frozen=True)
class CompactionArtifact:
    start: int
    end: int
    summary: str
