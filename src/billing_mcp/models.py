"""Modèle de domaine de la facturation.

Toutes les règles métier (TTC, reste dû, retard de paiement) sont centralisées ici
afin de rester testables indépendamment du protocole MCP.
"""

from __future__ import annotations
##__future__ 是"提前使用新特性"的开关，
# annotations 是具体要打开的那个特性，即"类型注解先不计算"。
# 主要好处是类里可以直接引用自己，不用给类型加引号。

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
from typing import Any, Final

CENT = Decimal("0.01")


class InvoiceStatus(str, Enum):
    """Cycle de vie d'une facture."""

    DRAFT = "draft"          # brouillon, non envoyée au client
    SENT = "sent"            # envoyée, en attente de règlement
    PAID = "paid"            # soldée
    CANCELLED = "cancelled"  # annulée / avoirée

    @classmethod
    def parse(cls, value: str) -> "InvoiceStatus":##属于类方法，调用写成 InvoiceStatus.parse
        ##把特殊字符串转换成枚举的工厂方法
        ##cls是类本身
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            valides = ", ".join(s.value for s in cls)
            raise ValueError(
                f"Statut inconnu : {value!r} (valeurs acceptées : {valides})"
            ) from exc


def to_money(value: Any) -> Decimal:
    """Convertit une valeur en montant arrondi au centime (ROUND_HALF_UP).

    On passe par ``str`` pour éviter les erreurs de représentation des float
    (``Decimal(0.1)`` != ``Decimal("0.1")``).
    """
    if isinstance(value, Decimal):
        montant = value
    else:
        try:
            montant = Decimal(str(value))
        except Exception as exc:  # noqa: BLE001 - on reformule l'erreur
            raise ValueError(f"Montant invalide : {value!r}") from exc
    return montant.quantize(CENT, rounding=ROUND_HALF_UP)##四舍五入


def parse_date(value: Any, champ: str = "date") -> date:
    """Parse une date ISO (``AAAA-MM-JJ``)."""
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(
            f"{champ} invalide : {value!r} (format attendu AAAA-MM-JJ)"
        ) from exc


@dataclass(frozen=True)
class Invoice:
    """Une facture client.

    ``amount_ht`` est le montant hors taxes, ``vat_rate`` le taux de TVA
    (0.20 pour 20 %) et ``paid_amount`` le cumul des règlements reçus.
    """

    id: str
    client: str
    issue_date: date
    due_date: date
    amount_ht: Decimal
    vat_rate: Decimal = Decimal("0.20")
    paid_amount: Decimal = Decimal("0.00")
    status: InvoiceStatus = InvoiceStatus.SENT
    currency: str = "EUR"
    label: str = ""

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValueError("L'identifiant de facture est obligatoire.")
        if not str(self.client).strip():
            raise ValueError(f"Facture {self.id} : le client est obligatoire.")
        if self.amount_ht < 0:
            raise ValueError(f"Facture {self.id} : le montant HT ne peut pas être négatif.")
        if self.vat_rate < 0:
            raise ValueError(f"Facture {self.id} : le taux de TVA ne peut pas être négatif.")
        if self.paid_amount < 0:
            raise ValueError(f"Facture {self.id} : le montant réglé ne peut pas être négatif.")
        if self.due_date < self.issue_date:
            raise ValueError(
                f"Facture {self.id} : l'échéance ({self.due_date}) précède "
                f"la date d'émission ({self.issue_date})."
            )

    # ------------------------------------------------------------------
    # Règles métier
    # ------------------------------------------------------------------
    @property
    ##把一个方法伪装成字段访问。  inv.amount_ttc 
    def amount_ttc(self) -> Decimal:
        """Montant TTC arrondi au centime."""
        return to_money(self.amount_ht * (Decimal("1") + self.vat_rate))

    @property
    def is_receivable(self) -> bool:
        """Une facture brouillon ou annulée ne constitue pas une créance."""
        return self.status not in (InvoiceStatus.DRAFT, InvoiceStatus.CANCELLED)

    @property
    def outstanding(self) -> Decimal:
        """Reste dû (jamais négatif : un trop-perçu n'est pas une créance)."""
        if not self.is_receivable:
            return Decimal("0.00")
        return max(Decimal("0.00"), to_money(self.amount_ttc - self.paid_amount))

    @property
    def is_unpaid(self) -> bool:
        """Impayée au sens large : il reste quelque chose à encaisser."""
        return self.outstanding > 0

    @property
    def is_partially_paid(self) -> bool:
        return self.is_unpaid and self.paid_amount > 0

    def is_overdue(self, as_of: date) -> bool:
        """Impayée *et* échéance dépassée. Le jour de l'échéance n'est pas en retard."""
        return self.is_unpaid and self.due_date < as_of

    def days_overdue(self, as_of: date) -> int:
        """Nombre de jours de retard (0 si la facture n'est pas en retard)."""
        if not self.is_overdue(as_of):
            return 0
        return (as_of - self.due_date).days

    # ------------------------------------------------------------------
    # Sérialisation
    # ------------------------------------------------------------------
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Invoice":
        manquants = [c for c in ("id", "client", "issue_date", "due_date", "amount_ht") if c not in data]
        if manquants:
            raise ValueError(f"Champs obligatoires manquants : {', '.join(manquants)}")
        return cls(
            id=str(data["id"]).strip(),
            client=str(data["client"]).strip(),
            issue_date=parse_date(data["issue_date"], "issue_date"),
            due_date=parse_date(data["due_date"], "due_date"),
            amount_ht=to_money(data["amount_ht"]),
            vat_rate=Decimal(str(data.get("vat_rate", "0.20"))),
            paid_amount=to_money(data.get("paid_amount", "0")),
            status=InvoiceStatus.parse(data.get("status", "sent")),
            currency=str(data.get("currency", "EUR")),
            label=str(data.get("label", "")),
        )

    def to_dict(self, as_of: date | None = None) -> dict[str, Any]:
        """Représentation JSON-compatible, enrichie des champs calculés."""
        as_of = as_of or date.today()
        return {
            "id": self.id,
            "client": self.client,
            "label": self.label,
            "issue_date": self.issue_date.isoformat(),
            "due_date": self.due_date.isoformat(),
            "amount_ht": str(self.amount_ht),
            "vat_rate": str(self.vat_rate),
            "amount_ttc": str(self.amount_ttc),
            "paid_amount": str(self.paid_amount),
            "outstanding": str(self.outstanding),
            "status": self.status.value,
            "currency": self.currency,
            "is_unpaid": self.is_unpaid,
            "is_overdue": self.is_overdue(as_of),
            "days_overdue": self.days_overdue(as_of),
        }