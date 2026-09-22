"""错误类型与错误语义。

所有由“程序本身不合法”引起的错误均为 :class:`InvalidProgramError`
（``ValueError`` 的子类），错误信息携带规则号 / 文字号 / 谓词等定位信息。
依赖图中存在含负边的环时抛出 :class:`StratificationError`。

预算耗尽不是异常：``evaluate`` 返回 ``status="incomplete"`` 的结果对象。
只有在不完整结果上试图把“查无此事实”当作“已证伪”时，才抛出
:class:`EvaluationIncomplete`，以区分“预算耗尽（未知）”与“证明无解（确证不存在）”。
"""


class DatalogError(Exception):
    """本库所有自定义错误的基类。"""


class InvalidProgramError(DatalogError, ValueError):
    """事实 / 规则 / 参数违反契约（结构、安全性、元数、数量上限等）。"""


class StratificationError(DatalogError, ValueError):
    """谓词依赖图中存在包含否定边的环，程序无法分层。"""


class EvaluationIncomplete(DatalogError):
    """在 ``status="incomplete"`` 的结果上请求了仅在完整模型下成立的结论。"""
