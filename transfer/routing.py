"""路由与防回环：纯逻辑，无 telethon 依赖，便于单测。

- parse_target_chats：解析 .env 里逗号分隔的多个目标 id。
- compute_targets：给定视频出现的来源，算出要无引用转发到哪些目标。
- SentRegistry：记录机器人自己发出的消息，事件回调命中即跳过，防止互为备份时死循环。
- OwnTextTracker：发送前按内容预登记自身文字，认出收藏夹里程序自己发的状态提醒。
"""
import time
from collections import deque


def parse_target_chats(raw):
    """把 `TARGET_CHAT` 的原始字符串解析成去重后的 int 列表（保持出现顺序）。

    支持英文逗号分隔，如 "-100111,-100222,-100333"。
    留空 / None → 返回 []（对应「仅下载」模式）。
    非数字项直接跳过（容错），避免一个笔误拖垮全部。
    """
    if not raw:
        return []
    out = []
    seen = set()
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            cid = int(part)
        except ValueError:
            continue
        if cid in seen:
            continue
        seen.add(cid)
        out.append(cid)
    return out


def compute_targets(source_id, me_id, target_ids):
    """算出一条视频应无引用转发到哪些目标。

    - 来源是收藏夹（source_id == me_id）→ 转发到 target_ids 全部；me 永不作为目标。
    - 来源是备份集合里的 X → 转发到「集合去掉 X」。
    始终排除 me 自身与来源自身，避免转回自己。
    """
    return [t for t in target_ids if t != source_id and t != me_id]


class SentRegistry:
    """记录机器人自己发出/转发出去的 (chat_id, message_id)，用于防回环。

    有界：只保留最近 maxlen 条，超出淘汰最旧的（这些 id 只需在「回声事件」到达
    的短窗口内有效）。重启清空即可，重启时不存在在途回声。
    """

    def __init__(self, maxlen=2000):
        self._maxlen = maxlen
        self._set = set()
        self._order = deque()

    def add(self, chat_id, message_id):
        if chat_id is None or message_id is None:
            return
        key = (chat_id, message_id)
        if key in self._set:
            return
        self._set.add(key)
        self._order.append(key)
        while len(self._order) > self._maxlen:
            self._set.discard(self._order.popleft())

    def add_messages(self, messages):
        """记录一次发送/转发返回的消息（单条或列表），鸭子类型读取 chat_id / id。"""
        if messages is None:
            return
        if not isinstance(messages, (list, tuple)):
            messages = [messages]
        for m in messages:
            if m is None:
                continue
            self.add(getattr(m, "chat_id", None), getattr(m, "id", None))

    def contains(self, chat_id, message_id):
        return (chat_id, message_id) in self._set


class OwnTextTracker:
    """记录程序自己即将发出的文字，用来在收藏夹里认出「自己发的状态提醒」。

    状态提醒的消息 id 要等 send_message 返回才知道，而它的 NewMessage 事件可能
    先到，SentRegistry 来不及登记。所以改在发送前按内容预登记：事件到达时内容
    命中就消费掉并跳过。每次登记只抵消一次；超过 ttl 秒未被消费的自动作废，
    避免发送失败的残留误伤之后用户手打的同样文字。
    """

    def __init__(self, ttl=120, maxlen=500, clock=None):
        self._ttl = ttl
        self._clock = clock or time.monotonic
        self._items = deque(maxlen=maxlen)   # [(text, 登记时间)]

    @staticmethod
    def _norm(text):
        return (text or "").strip()

    def mark(self, text):
        text = self._norm(text)
        if text:
            self._items.append((text, self._clock()))

    def consume(self, text):
        """text 是否为近期登记过的自身文字；命中则消费掉一次并返回 True。"""
        text = self._norm(text)
        if not text:
            return False
        now = self._clock()
        while self._items and now - self._items[0][1] > self._ttl:
            self._items.popleft()
        for i, (t, _) in enumerate(self._items):
            if t == text:
                del self._items[i]
                return True
        return False
