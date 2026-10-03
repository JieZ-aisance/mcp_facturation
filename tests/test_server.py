"""Tests d'intégration : les outils tels que Claude les appelle.

On passe par ``MCPServer.call_tool`` pour vérifier la couche protocole
(noms, schémas d'entrée, sortie structurée, gestion d'erreur).
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from .conftest import AS_OF

pytestmark = pytest.mark.asyncio

AS_OF_ISO = AS_OF.isoformat()


async def appeler(server_module, nom: str, **arguments: Any) -> dict[str, Any]:
    """Appelle un outil MCP et renvoie sa sortie structurée."""
    resultat = await server_module.mcp.call_tool(nom, arguments)
    assert not resultat.is_error, _message(resultat)
    if resultat.structured_content is not None:
        return resultat.structured_content
    return json.loads(resultat.content[0].text)


def _message(resultat) -> str:
    return " ".join(bloc.text for bloc in resultat.content if hasattr(bloc, "text"))


# `MCPServer.call_tool` relaie l'échec sous forme de ToolError ; côté protocole,
# le message est transmis au modèle dans un CallToolResult(is_error=True).


class TestDeclaration:
    async def test_outils_exposes(self, server_module):
        noms = {outil.name for outil in await server_module.mcp.list_tools()}
        assert noms == {
            "search_invoices",
            "get_invoice",
            "client_totals",
            "list_overdue_invoices",
            "aging_report",
            "list_clients",
        }

    async def test_chaque_outil_est_documente(self, server_module):
        """Sans description, Claude ne sait pas quand appeler l'outil."""
        for outil in await server_module.mcp.list_tools():
            assert outil.description and len(outil.description) > 20

    async def test_schema_dentree_des_parametres(self, server_module):
        outils = {o.name: o for o in await server_module.mcp.list_tools()}
        proprietes = outils["search_invoices"].input_schema["properties"]
        assert {"client", "status", "unpaid_only", "limit"} <= set(proprietes)
        # Aucun paramètre obligatoire : l'outil doit pouvoir être appelé à vide.
        assert not outils["search_invoices"].input_schema.get("required")


class TestRechercheFactures:
    async def test_sans_critere(self, server_module):
        sortie = await appeler(server_module, "search_invoices")
        assert sortie["count"] == 6

    async def test_par_client(self, server_module):
        sortie = await appeler(server_module, "search_invoices", client="dupont")
        assert [f["id"] for f in sortie["invoices"]] == ["FA-002"]

    async def test_impayees_uniquement(self, server_module):
        sortie = await appeler(server_module, "search_invoices", unpaid_only=True)
        assert {f["id"] for f in sortie["invoices"]} == {"FA-002", "FA-003", "FA-004"}

    async def test_limite_respectee(self, server_module):
        sortie = await appeler(server_module, "search_invoices", limit=2)
        assert sortie["count"] == 2 and len(sortie["invoices"]) == 2

    async def test_statut_invalide_renvoie_une_erreur_explicite(self, server_module):
        with pytest.raises(ToolError, match="Statut inconnu"):
            await server_module.mcp.call_tool("search_invoices", {"status": "zzz"})


class TestDetailFacture:
    async def test_facture_existante(self, server_module):
        sortie = await appeler(server_module, "get_invoice", invoice_id="FA-003")
        assert sortie["client"] == "Mairie de Vizille"
        assert sortie["amount_ttc"] == "4200.00"
        assert sortie["outstanding"] == "2100.00"

    async def test_facture_inconnue(self, server_module):
        with pytest.raises(ToolError, match="introuvable"):
            await server_module.mcp.call_tool("get_invoice", {"invoice_id": "FA-999"})


class TestTotauxClients:
    async def test_tous_les_clients(self, server_module):
        sortie = await appeler(server_module, "client_totals", as_of=AS_OF_ISO)
        assert sortie["client_count"] == 4
        assert sortie["clients"][0]["client"] == "SARL Dupont & Fils"
        assert sortie["clients"][0]["outstanding"] == "10080.00"

    async def test_un_client(self, server_module):
        sortie = await appeler(
            server_module, "client_totals", client="Café des Alpes", as_of=AS_OF_ISO
        )
        assert len(sortie["clients"]) == 1
        assert sortie["clients"][0]["outstanding"] == "0.00"

    async def test_client_inconnu(self, server_module):
        with pytest.raises(ToolError, match="Aucune facture"):
            await server_module.mcp.call_tool("client_totals", {"client": "Fantôme"})

    async def test_date_de_reference_invalide(self, server_module):
        with pytest.raises(ToolError, match="AAAA-MM-JJ"):
            await server_module.mcp.call_tool("client_totals", {"as_of": "03/10/2026"})


class TestImpayes:
    async def test_liste_et_total(self, server_module):
        sortie = await appeler(server_module, "list_overdue_invoices", as_of=AS_OF_ISO)
        assert sortie["count"] == 2
        assert sortie["total_overdue"] == "12180.00"
        assert [f["id"] for f in sortie["invoices"]] == ["FA-002", "FA-003"]
        assert sortie["invoices"][0]["days_overdue"] == 212

    async def test_filtre_par_client(self, server_module):
        sortie = await appeler(
            server_module, "list_overdue_invoices", as_of=AS_OF_ISO, client="vizille"
        )
        assert [f["id"] for f in sortie["invoices"]] == ["FA-003"]

    async def test_retard_minimal(self, server_module):
        sortie = await appeler(
            server_module, "list_overdue_invoices", as_of=AS_OF_ISO, min_days_overdue=100
        )
        assert sortie["count"] == 1

    async def test_aucun_retard_a_une_date_passee(self, server_module):
        sortie = await appeler(server_module, "list_overdue_invoices", as_of="2026-03-01")
        assert sortie["count"] == 0
        assert sortie["total_overdue"] == "0.00"


class TestBalanceAgeeEtClients:
    async def test_balance_agee(self, server_module):
        sortie = await appeler(server_module, "aging_report", as_of=AS_OF_ISO)
        assert sortie["as_of"] == AS_OF_ISO
        assert sortie["buckets"]["90+"]["amount"] == "10080.00"

    async def test_liste_des_clients(self, server_module):
        sortie = await appeler(server_module, "list_clients")
        assert sortie["count"] == 4
        assert "Café des Alpes" in sortie["clients"]


class TestRessourcesEtPrompts:
    async def test_ressource_facture(self, server_module):
        contenus = await server_module.mcp.read_resource("invoice://FA-002")
        charge = json.loads(list(contenus)[0].content)
        assert charge["client"] == "SARL Dupont & Fils"

    async def test_prompt_de_relance(self, server_module):
        resultat = await server_module.mcp.get_prompt("relance_client", {"client": "Dupont"})
        texte = resultat.messages[0].content.text
        assert "list_overdue_invoices" in texte and "Dupont" in texte