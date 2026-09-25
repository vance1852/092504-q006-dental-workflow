"""定义追溯账本在模块边界使用的数据对象。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CaseLedger:
    """质量视角下的单个病例完整账本。"""

    case: dict[str, Any]
    prescriptions: list[dict[str, Any]]
    routes: list[dict[str, Any]]
    branches: list[dict[str, Any]]
    products: list[dict[str, Any]]
    handoffs: list[dict[str, Any]]
    discrepancies: list[dict[str, Any]]
    material_lots: list[dict[str, Any]]
    material_splits: list[dict[str, Any]]
    material_consumptions: list[dict[str, Any]]


@dataclass(frozen=True)
class ProductTrace:
    """质量人员对任一成品的追溯结论。"""

    product: dict[str, Any]
    prescription: dict[str, Any]
    inputs: list[dict[str, Any]]
    handoff: dict[str, Any] | None
    branch: dict[str, Any] | None
    lineage: list[dict[str, Any]]
    remaining_materials: list[dict[str, Any]]
    open_discrepancies: list[dict[str, Any]]
