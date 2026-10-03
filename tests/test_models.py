"""Tests unitaires des règles métier de la facture."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from billing_mcp.models import Invoice, InvoiceStatus, to_money

from .conftest import AS_OF


class TestMontants:
    def test_ttc_applique_la_tva(self, invoice_factory):
        facture = invoice_factory(amount_ht="1000.00", vat_rate="0.20")
        assert facture.amount_ttc == Decimal("1200.00")

    @pytest.mark.parametrize(
        ("ht", "taux", "attendu"),
        [
            ("100.00", "0.20", "120.00"),
            ("0.00", "0.20", "0.00"),
            ("1200.00", "0.00", "1200.00"),
            ("640.00", "0.10", "704.00"),
            ("33.33", "0.20", "40.00"),      # 39.996 -> arrondi au centime supérieur
            ("10.125", "0.00", "10.13"),     # ROUND_HALF_UP et non "banker's rounding"
        ],
    )
    def test_arrondi_au_centime(self, invoice_factory, ht, taux, attendu):
        facture = invoice_factory(amount_ht=ht, vat_rate=taux)
        assert facture.amount_ttc == Decimal(attendu)

    def test_to_money_evite_les_erreurs_de_float(self):
        # Decimal(0.1) vaut 0.1000000000000000055..., str(0.1) vaut "0.1"
        assert to_money(0.1) == Decimal("0.10")
        assert to_money("2.675") == Decimal("2.68")

    def test_montant_invalide(self):
        with pytest.raises(ValueError, match="Montant invalide"):
            to_money("douze euros")


class TestResteDu:
    def test_facture_non_reglee(self, invoice_factory):
        facture = invoice_factory(amount_ht="1000.00")
        assert facture.outstanding == Decimal("1200.00")
        assert facture.is_unpaid

    def test_paiement_partiel(self, invoice_factory):
        facture = invoice_factory(amount_ht="1000.00", paid_amount="500.00")
        assert facture.outstanding == Decimal("700.00")
        assert facture.is_unpaid
        assert facture.is_partially_paid

    def test_facture_soldee(self, invoice_factory):
        facture = invoice_factory(amount_ht="1000.00", paid_amount="1200.00", status="paid")
        assert facture.outstanding == Decimal("0.00")
        assert not facture.is_unpaid

    def test_trop_percu_ne_cree_pas_de_creance_negative(self, invoice_factory):
        facture = invoice_factory(amount_ht="1000.00", paid_amount="1500.00", status="paid")
        assert facture.outstanding == Decimal("0.00")

    @pytest.mark.parametrize("statut", ["draft", "cancelled"])
    def test_brouillon_et_annulee_ne_sont_pas_des_creances(self, invoice_factory, statut):
        facture = invoice_factory(amount_ht="5000.00", status=statut)
        assert not facture.is_receivable
        assert facture.outstanding == Decimal("0.00")
        assert not facture.is_unpaid
        assert not facture.is_overdue(AS_OF)


class TestRetard:
    def test_echeance_future(self, invoice_factory):
        facture = invoice_factory(due_date="2026-12-31")
        assert not facture.is_overdue(AS_OF)
        assert facture.days_overdue(AS_OF) == 0

    def test_le_jour_de_lecheance_nest_pas_un_retard(self, invoice_factory):
        """Cas limite : le client a jusqu'à la fin du jour d'échéance pour payer."""
        facture = invoice_factory(due_date=AS_OF.isoformat())
        assert not facture.is_overdue(AS_OF)

    def test_premier_jour_de_retard(self, invoice_factory):
        facture = invoice_factory(due_date="2026-10-02")
        assert facture.is_overdue(AS_OF)
        assert facture.days_overdue(AS_OF) == 1

    def test_nombre_de_jours_de_retard(self, invoice_factory):
        facture = invoice_factory(issue_date="2026-08-01", due_date="2026-09-03")
        assert facture.days_overdue(AS_OF) == 30

    def test_facture_soldee_jamais_en_retard(self, invoice_factory):
        facture = invoice_factory(
            due_date="2026-01-31", amount_ht="100.00", paid_amount="120.00", status="paid"
        )
        assert not facture.is_overdue(AS_OF)


class TestValidation:
    def test_echeance_avant_emission(self, invoice_factory):
        with pytest.raises(ValueError, match="précède"):
            invoice_factory(issue_date="2026-05-01", due_date="2026-04-01")

    def test_montant_negatif(self, invoice_factory):
        with pytest.raises(ValueError, match="négatif"):
            invoice_factory(amount_ht="-10.00")

    def test_client_vide(self, invoice_factory):
        with pytest.raises(ValueError, match="client est obligatoire"):
            invoice_factory(client="   ")

    def test_champs_obligatoires_manquants(self):
        with pytest.raises(ValueError, match="Champs obligatoires manquants"):
            Invoice.from_dict({"id": "FA-999", "client": "X"})

    def test_statut_inconnu(self, invoice_factory):
        with pytest.raises(ValueError, match="Statut inconnu"):
            invoice_factory(status="en_attente")

    def test_date_invalide(self, invoice_factory):
        with pytest.raises(ValueError, match="format attendu"):
            invoice_factory(due_date="31/01/2026")

    def test_statut_tolere_la_casse(self, invoice_factory):
        assert invoice_factory(status=" PAID ").status is InvoiceStatus.PAID


class TestSerialisation:
    def test_to_dict_expose_les_champs_calcules(self, invoice_factory):
        facture = invoice_factory(
            id="FA-100", amount_ht="1000.00", paid_amount="200.00", due_date="2026-09-03"
        )
        donnees = facture.to_dict(AS_OF)
        assert donnees["id"] == "FA-100"
        assert donnees["amount_ttc"] == "1200.00"
        assert donnees["outstanding"] == "1000.00"
        assert donnees["is_overdue"] is True
        assert donnees["days_overdue"] == 30
        assert donnees["status"] == "sent"

    def test_montants_serialises_en_chaines(self, invoice_factory):
        """Les montants sortent en chaîne : pas de float dans le JSON renvoyé à Claude."""
        donnees = invoice_factory().to_dict(AS_OF)
        for champ in ("amount_ht", "amount_ttc", "paid_amount", "outstanding"):
            assert isinstance(donnees[champ], str)

    def test_aller_retour_dict(self, invoice_factory):
        facture = invoice_factory(id="FA-200", amount_ht="999.99")
        recharge = Invoice.from_dict(
            {
                "id": "FA-200",
                "client": facture.client,
                "issue_date": facture.issue_date.isoformat(),
                "due_date": facture.due_date.isoformat(),
                "amount_ht": "999.99",
            }
        )
        assert recharge.amount_ttc == facture.amount_ttc

    def test_dates_acceptees_en_objet_date(self, invoice_factory):
        facture = invoice_factory(issue_date=date(2026, 3, 1), due_date=date(2026, 3, 31))
        assert facture.issue_date == date(2026, 3, 1)