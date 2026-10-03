"""Tests unitaires du dépôt de factures."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from billing_mcp.repository import InvoiceRepository, normalize

from .conftest import AS_OF


class TestChargement:
    def test_depuis_fichier_json(self, data_file: Path):
        depot = InvoiceRepository.from_json_file(data_file)
        assert len(depot) == 6

    def test_accepte_une_liste_racine(self, tmp_path: Path, records: list[dict]):
        chemin = tmp_path / "liste.json"
        chemin.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        assert len(InvoiceRepository.from_json_file(chemin)) == len(records)

    def test_fichier_absent(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="introuvable"):
            InvoiceRepository.from_json_file(tmp_path / "nexiste_pas.json")

    def test_json_invalide(self, tmp_path: Path):
        chemin = tmp_path / "casse.json"
        chemin.write_text("{ceci n'est pas du json", encoding="utf-8")
        with pytest.raises(ValueError, match="JSON invalide"):
            InvoiceRepository.from_json_file(chemin)

    def test_ligne_invalide_signale_son_numero(self, records: list[dict]):
        records[1]["amount_ht"] = "-5"
        with pytest.raises(ValueError, match="Enregistrement n°2"):
            InvoiceRepository.from_records(records)

    def test_identifiants_en_double(self, records: list[dict]):
        records.append(dict(records[0]))
        with pytest.raises(ValueError, match="double"):
            InvoiceRepository.from_records(records)

    def test_fichier_de_donnees_livre_est_valide(self):
        """Le jeu d'exemple embarqué doit toujours se charger."""
        from billing_mcp.server import DEFAULT_DATA_FILE

        assert len(InvoiceRepository.from_json_file(DEFAULT_DATA_FILE)) > 0


class TestNormalisation:
    @pytest.mark.parametrize(
        ("saisie", "attendu"),
        [
            ("Café des Alpes", "cafe des alpes"),
            ("ÉLECTRO MÜLLER", "electro muller"),
            ("  Dupont  ", "dupont"),
        ],
    )
    def test_accents_et_casse(self, saisie, attendu):
        assert normalize(saisie) == attendu


class TestRecherche:
    def test_sans_critere_retourne_tout(self, repo):
        assert len(repo.search()) == 6

    def test_tri_par_date_demission_decroissante(self, repo):
        dates = [f.issue_date for f in repo.search()]
        assert dates == sorted(dates, reverse=True)

    def test_par_client_insensible_aux_accents(self, repo):
        resultats = repo.search(client="cafe")
        assert {f.id for f in resultats} == {"FA-001", "FA-005"}

    def test_par_fragment_de_client(self, repo):
        assert {f.id for f in repo.search(client="dupont")} == {"FA-002"}

    def test_recherche_libre_sur_le_libelle(self, repo):
        assert {f.id for f in repo.search(text="rgaa")} == {"FA-003"}

    def test_recherche_libre_sur_le_numero(self, repo):
        assert {f.id for f in repo.search(text="FA-004")} == {"FA-004"}

    def test_par_statut(self, repo):
        assert {f.id for f in repo.search(status="paid")} == {"FA-001"}
        assert {f.id for f in repo.search(status="draft")} == {"FA-006"}

    def test_statut_inconnu_leve_une_erreur(self, repo):
        with pytest.raises(ValueError, match="Statut inconnu"):
            repo.search(status="archivee")

    def test_par_periode_demission(self, repo):
        resultats = repo.search(issued_from="2026-02-01", issued_to="2026-02-28")
        assert {f.id for f in resultats} == {"FA-002", "FA-003"}

    def test_bornes_de_periode_inclusives(self, repo):
        resultats = repo.search(issued_from="2026-02-03", issued_to="2026-02-03")
        assert {f.id for f in resultats} == {"FA-002"}

    def test_periode_incoherente(self, repo):
        with pytest.raises(ValueError, match="issued_from"):
            repo.search(issued_from="2026-05-01", issued_to="2026-01-01")

    def test_par_montant_ttc(self, repo):
        resultats = repo.search(min_amount_ttc=5000)
        assert {f.id for f in resultats} == {"FA-002", "FA-006"}

    def test_bornes_de_montant_incoherentes(self, repo):
        with pytest.raises(ValueError, match="min_amount_ttc"):
            repo.search(min_amount_ttc=1000, max_amount_ttc=10)

    def test_impayees_uniquement(self, repo):
        """Exclut la facture soldée, le brouillon et l'annulée."""
        assert {f.id for f in repo.search(unpaid_only=True)} == {"FA-002", "FA-003", "FA-004"}

    def test_criteres_combines(self, repo):
        resultats = repo.search(client="bonnet", unpaid_only=True)
        assert {f.id for f in resultats} == {"FA-004"}

    def test_limite(self, repo):
        assert len(repo.search(limit=2)) == 2

    def test_limite_invalide(self, repo):
        with pytest.raises(ValueError, match="limit"):
            repo.search(limit=0)

    def test_aucun_resultat(self, repo):
        assert repo.search(client="client inexistant") == []


class TestGet:
    def test_par_identifiant(self, repo):
        assert repo.get("FA-002").client == "SARL Dupont & Fils"

    def test_insensible_a_la_casse_et_aux_espaces(self, repo):
        assert repo.get("  fa-002 ").id == "FA-002"

    def test_identifiant_inconnu(self, repo):
        assert repo.get("FA-999") is None


class TestTotauxClients:
    def test_totaux_dun_client(self, repo):
        totaux = repo.totals_for_client("Mairie de Vizille", AS_OF)
        assert totaux.invoice_count == 1
        assert totaux.total_ttc == Decimal("4200.00")
        assert totaux.paid == Decimal("2100.00")
        assert totaux.outstanding == Decimal("2100.00")
        assert totaux.overdue == Decimal("2100.00")

    def test_annulee_exclue_du_reste_du(self, repo):
        """Café des Alpes : une facture soldée + une annulée => rien à encaisser."""
        totaux = repo.totals_for_client("Café des Alpes", AS_OF)
        assert totaux.invoice_count == 2
        assert totaux.outstanding == Decimal("0.00")
        assert totaux.overdue_count == 0

    def test_brouillon_compte_dans_le_facture_mais_pas_dans_le_du(self, repo):
        totaux = repo.totals_for_client("Transports Bonnet", AS_OF)
        assert totaux.invoice_count == 2
        assert totaux.total_ttc == Decimal("14280.00")  # 4920 + 9360
        assert totaux.outstanding == Decimal("3920.00")  # seul le lot 2, moins l'acompte

    def test_correspondance_partielle(self, repo):
        assert repo.totals_for_client("dupont", AS_OF).client == "SARL Dupont & Fils"

    def test_client_inconnu(self, repo):
        with pytest.raises(LookupError, match="Aucune facture"):
            repo.totals_for_client("Entreprise Fantôme", AS_OF)

    def test_tous_les_clients_tries_par_reste_du(self, repo):
        totaux = repo.totals_by_client(AS_OF)
        assert [t.client for t in totaux][:3] == [
            "SARL Dupont & Fils",   # 10 080 €
            "Transports Bonnet",    #  3 920 €
            "Mairie de Vizille",    #  2 100 €
        ]
        assert totaux[-1].outstanding == Decimal("0.00")

    def test_somme_des_restes_dus(self, repo):
        total = sum((t.outstanding for t in repo.totals_by_client(AS_OF)), Decimal("0.00"))
        assert total == Decimal("16100.00")


class TestImpayes:
    def test_liste_des_retards(self, repo):
        assert [f.id for f in repo.overdue(AS_OF)] == ["FA-002", "FA-003"]

    def test_tri_du_retard_le_plus_ancien_au_plus_recent(self, repo):
        retards = repo.overdue(AS_OF)
        jours = [f.days_overdue(AS_OF) for f in retards]
        assert jours == sorted(jours, reverse=True)

    def test_retard_minimal(self, repo):
        assert [f.id for f in repo.overdue(AS_OF, min_days_overdue=30)] == ["FA-002"]

    def test_retard_minimal_negatif(self, repo):
        with pytest.raises(ValueError, match="min_days_overdue"):
            repo.overdue(AS_OF, min_days_overdue=-1)

    def test_filtre_par_client(self, repo):
        assert [f.id for f in repo.overdue(AS_OF, client="vizille")] == ["FA-003"]

    def test_limite(self, repo):
        assert len(repo.overdue(AS_OF, limit=1)) == 1

    def test_date_de_reference_passee(self, repo):
        """Au 1er mars 2026, aucune facture n'est encore en retard."""
        from datetime import date

        assert repo.overdue(date(2026, 3, 1)) == []


class TestBalanceAgee:
    def test_ventilation_par_tranche(self, repo):
        tranches = repo.aging_buckets(AS_OF)
        assert tranches["non_echu"]["invoice_ids"] == ["FA-004"]      # échéance 19/11
        assert tranches["1-30"]["invoice_ids"] == ["FA-003"]          # 20 jours
        assert tranches["90+"]["invoice_ids"] == ["FA-002"]           # 212 jours
        assert tranches["31-60"]["invoice_count"] == 0

    def test_montants_par_tranche(self, repo):
        tranches = repo.aging_buckets(AS_OF)
        assert tranches["90+"]["amount"] == "10080.00"
        assert tranches["1-30"]["amount"] == "2100.00"
        assert tranches["non_echu"]["amount"] == "3920.00"

    def test_chaque_facture_dans_une_seule_tranche(self, repo):
        tranches = repo.aging_buckets(AS_OF)
        ids = [i for case in tranches.values() for i in case["invoice_ids"]]
        assert len(ids) == len(set(ids)) == 3  # les 3 factures avec un reste dû