"""Serveur MCP « assistant de facturation ».

Expose à Claude des outils de consultation des factures :
recherche multicritère, totaux par client, détection des impayés et balance âgée.

Lancement :
    python -m billing_mcp              # transport stdio (Claude Desktop / Claude Code)
    BILLING_DATA_FILE=./factures.json python -m billing_mcp
"""

from __future__ import annotations

import functools
import os
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError

from .models import parse_date
from .repository import InvoiceRepository

DEFAULT_DATA_FILE = Path(__file__).parent / "data" / "invoices.json"

mcp = MCPServer(
    name="assistant-facturation",
    instructions=(
        "Outils de consultation d'une base de factures clients. "
        "Les montants sont renvoyés sous forme de chaînes décimales (ex. \"1440.00\") "
        "pour éviter toute perte de précision ; la devise est indiquée par facture. "
        "Les dates sont au format AAAA-MM-JJ. Ce serveur est en lecture seule : "
        "il ne crée, ne modifie et ne supprime aucune facture."
    ),
)


@lru_cache(maxsize=1)
def get_repository() -> InvoiceRepository:
    """Charge le dépôt une seule fois par processus."""
    chemin = os.environ.get("BILLING_DATA_FILE", str(DEFAULT_DATA_FILE))
    return InvoiceRepository.from_json_file(chemin)


def _as_of(value: str | None) -> date:
    """Date de référence des calculs de retard (aujourd'hui par défaut)."""
    return parse_date(value, "as_of") if value else date.today()


F = TypeVar("F", bound=Callable[..., Any])


def business_errors(func: F) -> F:
    """Convertit les erreurs métier en ToolError.

    Sans cela, une ValueError est traitée comme un plantage : le modèle ne reçoit
    qu'un « Error executing tool » générique au lieu du message explicatif.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (ValueError, LookupError, FileNotFoundError) as exc:
            raise ToolError(str(exc)) from exc

    return wrapper  # type: ignore[return-value]


# ----------------------------------------------------------------------
# Outils
# ----------------------------------------------------------------------
@mcp.tool()
@business_errors
def search_invoices(
    client: str | None = None,
    text: str | None = None,
    status: str | None = None,
    issued_from: str | None = None,
    issued_to: str | None = None,
    min_amount_ttc: float | None = None,
    max_amount_ttc: float | None = None,
    unpaid_only: bool = False,
    limit: int = 20,
) -> dict[str, Any]:
    """Recherche des factures selon plusieurs critères combinables.

    Args:
        client: nom (ou fragment) du client, insensible à la casse et aux accents.
        text: recherche libre sur le numéro, le client et le libellé.
        status: draft, sent, paid ou cancelled.
        issued_from: date d'émission minimale (AAAA-MM-JJ).
        issued_to: date d'émission maximale (AAAA-MM-JJ).
        min_amount_ttc: montant TTC minimal.
        max_amount_ttc: montant TTC maximal.
        unpaid_only: ne garder que les factures avec un reste dû.
        limit: nombre maximal de résultats (20 par défaut).
    """
    depot = get_repository()
    aujourdhui = date.today()
    factures = depot.search(
        client=client,
        text=text,
        status=status,
        issued_from=issued_from,
        issued_to=issued_to,
        min_amount_ttc=min_amount_ttc,
        max_amount_ttc=max_amount_ttc,
        unpaid_only=unpaid_only,
        limit=limit,
    )
    return {
        "count": len(factures),
        "limit": limit,
        "invoices": [f.to_dict(aujourdhui) for f in factures],
    }


@mcp.tool()
@business_errors
def get_invoice(invoice_id: str) -> dict[str, Any]:
    """Renvoie le détail d'une facture à partir de son numéro (ex. FA-2026-002)."""
    facture = get_repository().get(invoice_id)
    if facture is None:
        raise ValueError(f"Facture introuvable : {invoice_id!r}")
    return facture.to_dict(date.today())


@mcp.tool()
@business_errors
def client_totals(client: str | None = None, as_of: str | None = None) -> dict[str, Any]:
    """Totaux facturés, encaissés et restant dus, par client.

    Args:
        client: nom du client ; si omis, renvoie tous les clients classés par reste dû.
        as_of: date de référence pour le calcul des retards (aujourd'hui par défaut).
    """
    depot = get_repository()
    reference = _as_of(as_of)
    if client:
        totaux = depot.totals_for_client(client, reference)
        return {"as_of": reference.isoformat(), "clients": [totaux.to_dict()]}
    tous = depot.totals_by_client(reference)
    return {
        "as_of": reference.isoformat(),
        "client_count": len(tous),
        "clients": [t.to_dict() for t in tous],
    }


@mcp.tool()
@business_errors
def list_overdue_invoices(
    as_of: str | None = None,
    client: str | None = None,
    min_days_overdue: int = 1,
    limit: int = 50,
) -> dict[str, Any]:
    """Liste les factures impayées dont l'échéance est dépassée, les plus anciennes d'abord.

    Args:
        as_of: date de référence (aujourd'hui par défaut).
        client: restreindre à un client.
        min_days_overdue: retard minimal en jours (1 par défaut).
        limit: nombre maximal de résultats.
    """
    depot = get_repository()
    reference = _as_of(as_of)
    factures = depot.overdue(
        reference, client=client, min_days_overdue=min_days_overdue, limit=limit
    )
    total = sum((f.outstanding for f in factures), Decimal("0.00"))
    return {
        "as_of": reference.isoformat(),
        "count": len(factures),
        "total_overdue": str(total),
        "invoices": [f.to_dict(reference) for f in factures],
    }


@mcp.tool()
@business_errors
def aging_report(as_of: str | None = None) -> dict[str, Any]:
    """Balance âgée : ventilation du reste dû par tranche d'ancienneté (non échu, 1-30, 31-60, 61-90, 90+)."""
    reference = _as_of(as_of)
    return {
        "as_of": reference.isoformat(),
        "buckets": get_repository().aging_buckets(reference),
    }


@mcp.tool()
@business_errors
def list_clients() -> dict[str, Any]:
    """Liste les clients présents dans la base de factures."""
    clients = get_repository().clients()
    return {"count": len(clients), "clients": clients}


# ----------------------------------------------------------------------
# Ressources et prompts
# ----------------------------------------------------------------------
@mcp.resource("invoice://{invoice_id}", mime_type="application/json")
def invoice_resource(invoice_id: str) -> dict[str, Any]:
    """Facture individuelle exposée comme ressource."""
    facture = get_repository().get(invoice_id)
    if facture is None:
        raise ResourceNotFoundError(f"Facture introuvable : {invoice_id!r}")
    return facture.to_dict(date.today())


@mcp.prompt()
def relance_client(client: str, ton: str = "courtois") -> str:
    """Rédige un courriel de relance pour les impayés d'un client."""
    return (
        f"Utilise list_overdue_invoices avec client=\"{client}\" puis rédige un courriel "
        f"de relance en français, sur un ton {ton}. Rappelle le numéro, la date d'échéance "
        f"et le montant restant dû de chaque facture, indique le total, et propose un "
        f"règlement sous 8 jours. N'invente aucun montant : utilise uniquement ceux "
        f"renvoyés par l'outil."
    )


def main() -> None:
    """Point d'entrée : démarre le serveur sur le transport stdio."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()