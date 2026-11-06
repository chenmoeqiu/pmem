"""导入器：把外部数据源转换成中立的记忆记录。

每个适配器只做一件事：读文件 -> 逐轮产出 (会话, 消息, 附件)。
入库逻辑统一在 store 层，因此新增数据源只需要新增一个适配器。
"""

from . import takeout, voyager  # noqa: F401

__all__ = ["takeout", "voyager"]
