# -*- coding: utf-8 -*-
"""API 数据模型。"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class AskRequest(BaseModel):
    question: str = Field(..., description="自然语言问题")
    user_id: str = Field("guest", description="用户 ID（上线后由 SSO 注入，不信任前端传值）；"
                                              "默认 guest = 最小权限，禁止默认成任何真实角色")
    dsl: Optional[Dict[str, Any]] = Field(
        None, description="已知口径直接注入（调试/回放用，绕过 LLM）")
    resolved: Optional[Dict[str, str]] = Field(None, description="澄清结果 {规则ID: 选择值}")
    session_id: Optional[str] = Field(
        None, description="会话 ID（F1.9）。不传则自动新建一个会话，仍可用")
    follow_up: bool = Field(
        True, description="是否继承本会话上文（F1.2 指代消解）。关闭则每一问都独立解析")


class SessionIn(BaseModel):
    user_id: str = "guest"
    title: Optional[str] = None


class FeedbackRequest(BaseModel):
    answer_id: Optional[str] = None
    user_id: str = "guest"
    kind: str = Field(..., description="correct / wrong_number / wrong_caliber / unclear")
    comment: Optional[str] = None
    question: Optional[str] = None


class BacklogRequest(BaseModel):
    question: str
    user_id: str = "guest"
    reason: str = "未命中"


class PrefRequest(BaseModel):
    user_id: str
    scope: str = Field(..., description="指标域或指标码")
    key: str
    value: str


class MetricIn(BaseModel):
    """指标注册/更新的输入。**语义：`None` = 未提供 = 保留原值**（`registry.upsert_metric`）。

    ⚠️ 三个字段的默认值刻意不给"看起来合理"的取值（`type="ratio"` / `sensitivity="public"`
       / `status="online"`）：它们曾经是默认值，于是任何只发 `{code, name, owner}` 的调用
       都会把指标的敏感级从 `restricted` 悄悄降成 `public`、把已下线的指标重新上线，
       而版本记录只会写"改责任人"。默认值改在**新建**时由 `upsert_metric` 补，
       更新路径一律"没说就不动"。
    """

    # 注意：以下三个字段**不是指标内容**，写库前会被排除（见 semantic.upsert 的 exclude）
    user_id: Optional[str] = Field(
        None, description="仅演示模式使用：声明调用者身份；生产模式忽略（只认网关注入的可信头）")
    operator: str = "system"
    change_note: Optional[str] = ""

    code: str
    name: str
    type: Optional[str] = None
    domain: Optional[str] = None
    definition: Optional[str] = None
    formula: Optional[str] = None
    owner: Optional[str] = None
    alias: Optional[List[str]] = None
    supported_date_basis: Optional[List[str]] = None
    supported_dept_basis: Optional[List[str]] = None
    sensitivity: Optional[str] = None
    status: Optional[str] = None


class RetireRequest(BaseModel):
    user_id: Optional[str] = Field(
        None, description="仅演示模式使用：声明调用者身份；生产模式忽略")
    operator: str = "system"
    reason: str = ""


class ExportRequest(BaseModel):
    answer_id: Optional[str] = None
    user_id: str = "guest"
    fmt: str = "csv"
