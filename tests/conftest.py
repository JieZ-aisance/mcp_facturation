"""Fixtures partagées par les tests."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from billing_mcp.models import Invoice
from billing_mcp.repository import InvoiceRepository

# Date de référence figée : les tests ne doivent pas dépendre du jour d'exécution.
AS_OF = date(2026, 10, 3)


RECORDS: list[dict] = [
    {   # soldée
        "id": "FA-001",
        "client": "Café des Alpes",
        "label": "Maintenance T1",
        "issue_date": "2026-01-15",
        "due_date": "2026-02-14",
        "amount_ht": "1200.00",
        "paid_amount": "1440.00",
        "status": "paid",
    },
    {   # impayée, très en retard
        "id": "FA-002",
        "client": "SARL Dupont & Fils",
        "label": "Module stock",
        "issue_date": "2026-02-03",
        "due_date": "2026-03-05",
        "amount_ht": "8400.00",
        "paid_amount": "0.00",
        "status": "sent",
    },
    {   # partiellement payée, en retard
        "id": "FA-003",
        "client": "Mairie de Vizille",
        "label": "Audit RGAA",
        "issue_date": "2026-02-20",
        "due_date": "2026-09-13",
        "amount_ht": "3500.00",
        "paid_amount": "2100.00",
        "status": "sent",
    },
    {   # échéance dans le futur : non échue
        "id": "FA-004",
        "client": "Transports Bonnet",
        "label": "Portail lot 2",
        "issue_date": "2026-09-20",
        "due_date": "2026-11-19",
        "amount_ht": "4100.00",
        "paid_amount": "1000.00",
        "status": "sent",
    },
    {   # annulée : ne doit jamais compter comme créance
        "id": "FA-005",
        "client": "Café des Alpes",
        "label": "Refonte carte",
        "issue_date": "2026-04-25",
        "due_date": "2026-05-25",
        "amount_ht": "640.00",
        "vat_rate": "0.10",
        "paid_amount": "0.00",
        "status": "cancelled",
    },
    {   # brouillon : pas encore une créance
        "id": "FA-006",
        "client": "Transports Bonnet",
        "label": "Devis lot 3",
        "issue_date": "2026-09-28",
        "due_date": "2026-10-28",
        "amount_ht": "7800.00",
        "paid_amount": "0.00",
        "status": "draft",
    },
]


@pytest.fixture
def records() -> list[dict]:
    return [dict(r) for r in RECORDS]


@pytest.fixture
def repo(records: list[dict]) -> InvoiceRepository:
    return InvoiceRepository.from_records(records)


@pytest.fixture
def invoice_factory():
    """Fabrique de factures avec des valeurs par défaut raisonnables."""

    def _make(**overrides) -> Invoice:
        base = {
            "id": "FA-TEST",
            "client": "Client Test",
            "issue_date": "2026-01-01",
            "due_date": "2026-01-31",
            "amount_ht": "100.00",
            "vat_rate": "0.20",
            "paid_amount": "0.00",
            "status": "sent",
        }
        base.update(overrides)
        return Invoice.from_dict(base)

    return _make


@pytest.fixture
def data_file(tmp_path: Path, records: list[dict]) -> Path:
    """Écrit le jeu de test dans un fichier JSON temporaire."""
    chemin = tmp_path / "invoices.json"
    chemin.write_text(json.dumps({"invoices": records}, ensure_ascii=False), encoding="utf-8")
    return chemin


@pytest.fixture
def server_module(data_file: Path, monkeypatch: pytest.MonkeyPatch):
    """Serveur MCP branché sur les données de test (cache de dépôt vidé)."""
    from billing_mcp import server

    monkeypatch.setenv("BILLING_DATA_FILE", str(data_file))
    server.get_repository.cache_clear()
    yield server
    server.get_repository.cache_clear()