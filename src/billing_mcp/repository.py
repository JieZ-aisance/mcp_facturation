"""Accès aux données de facturation.
管一堆发票，负责加载、查找、筛选、汇总
Le dépôt travaille en mémoire à partir d'un fichier JSON. Il serait remplacé par
un accès base de données sans changer l'interface utilisée par le serveur MCP.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Sequence

from .models import Invoice, InvoiceStatus, parse_date, to_money

ZERO = Decimal("0.00")


def normalize(text: str) -> str:
    """Minuscules sans accents, pour une recherche tolérante.
    模糊搜索用的文本标准化
    « Café des Alpes » doit être trouvé en tapant « cafe ».
    """
    decompose = unicodedata.normalize("NFKD", str(text))
    sans_accents = "".join(c for c in decompose if not unicodedata.combining(c))
    return sans_accents.casefold().strip()


@dataclass(frozen=True)
class ClientTotals:
    """Agrégats de facturation pour un client."""
###mcp返回的就是他，不可变的dataclass
    client: str
    invoice_count: int
    total_ht: Decimal
    total_ttc: Decimal
    paid: Decimal
    outstanding: Decimal
    overdue: Decimal
    overdue_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "client": self.client,
            "invoice_count": self.invoice_count,
            "total_ht": str(self.total_ht),
            "total_ttc": str(self.total_ttc),
            "paid": str(self.paid),
            "outstanding": str(self.outstanding),
            "overdue": str(self.overdue),
            "overdue_count": self.overdue_count,
        }


class InvoiceRepository:
    """Collection de factures interrogeable."""

    def __init__(self, invoices: Iterable[Invoice]) -> None:
        self._invoices: list[Invoice] = list(invoices)
        doublons = self._find_duplicates(self._invoices)
        if doublons:
            raise ValueError(f"Identifiants de facture en double : {', '.join(sorted(doublons))}")
        self._by_id = {f.id: f for f in self._invoices}

    @staticmethod
    def _find_duplicates(invoices: Sequence[Invoice]) -> set[str]:
        vus: set[str] = set()
        doublons: set[str] = set()
        for facture in invoices:
            if facture.id in vus:
                doublons.add(facture.id)
            vus.add(facture.id)
        return doublons

    # ------------------------------------------------------------------
    # Chargement
    # ------------------------------------------------------------------
    @classmethod
    def from_records(cls, records: Iterable[dict[str, Any]]) -> "InvoiceRepository":
        factures = []
        for index, record in enumerate(records, start=1):
            try:
                factures.append(Invoice.from_dict(record))
            except ValueError as exc:
                raise ValueError(f"Enregistrement n°{index} invalide : {exc}") from exc
        return cls(factures)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "InvoiceRepository":
        chemin = Path(path)
        if not chemin.is_file():
            raise FileNotFoundError(f"Fichier de données introuvable : {chemin}")
        try:
            donnees = json.loads(chemin.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSON invalide dans {chemin} : {exc}") from exc
        if isinstance(donnees, dict):
            donnees = donnees.get("invoices", [])
        if not isinstance(donnees, list):
            raise ValueError(f"{chemin} doit contenir une liste de factures.")
        return cls.from_records(donnees)

    # ------------------------------------------------------------------
    # Lecture
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._invoices)

    @property
    def invoices(self) -> list[Invoice]:
        return list(self._invoices)

    def get(self, invoice_id: str) -> Invoice | None:
        """Recherche par identifiant, insensible à la casse et aux espaces."""
        cible = str(invoice_id).strip()
        facture = self._by_id.get(cible)
        if facture is not None:
            return facture
        for candidat in self._invoices:
            if candidat.id.casefold() == cible.casefold():
                return candidat
        return None

    def clients(self) -> list[str]:
        return sorted({f.client for f in self._invoices}, key=normalize)

    def search(
        self,
        *,
        client: str | None = None,
        text: str | None = None,
        status: str | InvoiceStatus | None = None,
        issued_from: date | str | None = None,
        issued_to: date | str | None = None,
        min_amount_ttc: Decimal | str | float | None = None,
        max_amount_ttc: Decimal | str | float | None = None,
        unpaid_only: bool = False,
        limit: int | None = None,
    ) -> list[Invoice]:
        """Recherche multicritère. Résultats triés par date d'émission décroissante."""
        if limit is not None and limit <= 0:
            raise ValueError("limit doit être strictement positif.")

        statut = InvoiceStatus.parse(status) if status is not None else None
        depuis = parse_date(issued_from, "issued_from") if issued_from else None
        jusqua = parse_date(issued_to, "issued_to") if issued_to else None
        if depuis and jusqua and depuis > jusqua:
            raise ValueError("issued_from doit être antérieure ou égale à issued_to.")
        montant_min = to_money(min_amount_ttc) if min_amount_ttc is not None else None
        montant_max = to_money(max_amount_ttc) if max_amount_ttc is not None else None
        if montant_min is not None and montant_max is not None and montant_min > montant_max:
            raise ValueError("min_amount_ttc doit être inférieur ou égal à max_amount_ttc.")

        motif_client = normalize(client) if client else None
        motif_texte = normalize(text) if text else None

        resultats = []
        for facture in self._invoices:
            if motif_client and motif_client not in normalize(facture.client):
                continue
            if motif_texte and motif_texte not in normalize(
                f"{facture.id} {facture.client} {facture.label}"
            ):
                continue
            if statut is not None and facture.status is not statut:
                continue
            if depuis and facture.issue_date < depuis:
                continue
            if jusqua and facture.issue_date > jusqua:
                continue
            if montant_min is not None and facture.amount_ttc < montant_min:
                continue
            if montant_max is not None and facture.amount_ttc > montant_max:
                continue
            if unpaid_only and not facture.is_unpaid:
                continue
            resultats.append(facture)

        resultats.sort(key=lambda f: (f.issue_date, f.id), reverse=True)
        return resultats[:limit] if limit else resultats

    def overdue(
        self,
        as_of: date,
        *,
        client: str | None = None,
        min_days_overdue: int = 1,
        limit: int | None = None,
    ) -> list[Invoice]:
        """Factures en retard de paiement, de la plus ancienne à la plus récente."""
        if min_days_overdue < 0:
            raise ValueError("min_days_overdue ne peut pas être négatif.")
        motif = normalize(client) if client else None
        retards = [
            f
            for f in self._invoices
            if f.is_overdue(as_of)
            and f.days_overdue(as_of) >= min_days_overdue
            and (motif is None or motif in normalize(f.client))
        ]
        retards.sort(key=lambda f: (-f.days_overdue(as_of), f.id))
        return retards[:limit] if limit else retards

    def totals_for_client(self, client: str, as_of: date) -> ClientTotals:
        factures = [f for f in self._invoices if normalize(f.client) == normalize(client)]
        if not factures:
            # Repli sur une correspondance partielle (« Dupont » -> « SARL Dupont »)
            factures = [f for f in self._invoices if normalize(client) in normalize(f.client)]
        if not factures:
            raise LookupError(f"Aucune facture pour le client {client!r}.")
        nom = factures[0].client
        return self._aggregate(nom, factures, as_of)

    def totals_by_client(self, as_of: date) -> list[ClientTotals]:
        """Agrégats pour tous les clients, du plus gros reste dû au plus petit."""
        groupes: dict[str, list[Invoice]] = {}
        for facture in self._invoices:
            groupes.setdefault(facture.client, []).append(facture)
        totaux = [self._aggregate(nom, lot, as_of) for nom, lot in groupes.items()]
        totaux.sort(key=lambda t: (-t.outstanding, -t.total_ttc, normalize(t.client)))
        return totaux

    @staticmethod
    def _aggregate(client: str, factures: Sequence[Invoice], as_of: date) -> ClientTotals:
        en_retard = [f for f in factures if f.is_overdue(as_of)]
        return ClientTotals(
            client=client,
            invoice_count=len(factures),
            total_ht=to_money(sum((f.amount_ht for f in factures), ZERO)),
            total_ttc=to_money(sum((f.amount_ttc for f in factures), ZERO)),
            paid=to_money(sum((f.paid_amount for f in factures), ZERO)),
            outstanding=to_money(sum((f.outstanding for f in factures), ZERO)),
            overdue=to_money(sum((f.outstanding for f in en_retard), ZERO)),
            overdue_count=len(en_retard),
        )

    def aging_buckets(self, as_of: date) -> dict[str, dict[str, Any]]:
        """Balance âgée : ventilation du reste dû par ancienneté de la créance."""
        tranches: list[tuple[str, int, int | None]] = [
            ("non_echu", -10**6, 0),
            ("1-30", 1, 30),
            ("31-60", 31, 60),
            ("61-90", 61, 90),
            ("90+", 91, None),
        ]
        resultat = {
            nom: {"amount": ZERO, "invoice_count": 0, "invoice_ids": []} for nom, _, _ in tranches
        }
        for facture in self._invoices:
            if not facture.is_unpaid:
                continue
            jours = (as_of - facture.due_date).days  # négatif = pas encore échu
            for nom, mini, maxi in tranches:
                if jours >= mini and (maxi is None or jours <= maxi):
                    case = resultat[nom]
                    case["amount"] += facture.outstanding
                    case["invoice_count"] += 1
                    case["invoice_ids"].append(facture.id)
                    break
        for case in resultat.values():
            case["amount"] = str(to_money(case["amount"]))
        return resultat