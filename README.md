# Assistant de facturation — serveur MCP

Serveur MCP en Python permettant à Claude d'interroger des données de facturation : recherche de factures, totaux par client, détection des impayés.

Le serveur implémente le [Model Context Protocol](https://modelcontextprotocol.io) : une fois branché sur Claude Desktop ou Claude Code, on peut poser des questions en langage naturel (« Quels clients ont des factures en retard de plus de 30 jours ? ») et Claude appelle lui-même les bons outils.

## Fonctionnalités

| Outil | Rôle |
|---|---|
| `search_invoices` | Recherche multicritère : client, texte libre, statut, période d'émission, fourchette de montant TTC, impayées seulement |
| `get_invoice` | Détail d'une facture à partir de son numéro |
| `client_totals` | Facturé HT/TTC, encaissé, reste dû et montant en retard, par client |
| `list_overdue_invoices` | Factures impayées à échéance dépassée, les plus anciennes d'abord, avec le total dû |
| `aging_report` | Balance âgée : reste dû par tranche (non échu, 1-30, 31-60, 61-90, 90+ jours) |
| `list_clients` | Liste des clients présents dans la base |

Le serveur expose aussi :

- une **ressource** `invoice://{invoice_id}` (une facture au format JSON) ;
- un **prompt** `relance_client`, qui fait rédiger à Claude un courriel de relance à partir des impayés réels du client, sans inventer de montant.

Le serveur est en **lecture seule** : aucun outil ne crée, ne modifie ou ne supprime de facture.

## Choix techniques

- **Montants en `Decimal`**, arrondis au centime (`ROUND_HALF_UP`), et renvoyés sous forme de chaînes (`"1440.00"`) pour éviter toute perte de précision liée aux `float`.
- **Règles métier centralisées** dans `models.py` (TTC, reste dû, retard) et testées indépendamment du protocole MCP. Une facture brouillon ou annulée n'est jamais comptée comme créance ; un trop-perçu ne produit pas de reste dû négatif ; le jour de l'échéance n'est pas un retard.
- **Date de référence `as_of`** paramétrable : les calculs de retard sont reproductibles, en test comme en production.
- **Erreurs métier lisibles par le modèle** : une facture introuvable, une date ou un statut invalide sont convertis en `ToolError` avec un message explicite, au lieu d'une erreur générique.
- **Recherche insensible à la casse et aux accents** (« cafe des alpes » trouve « Café des Alpes »).
- **Données validées au chargement** : champs obligatoires, montants négatifs, échéance antérieure à l'émission, identifiants en double.
- **Couche d'accès isolée** (`InvoiceRepository`) : la source JSON pourrait être remplacée par une base de données sans toucher au serveur.

## Structure

```
src/billing_mcp/
├── models.py        # domaine : Invoice, statuts, règles métier
├── repository.py    # chargement, recherche, agrégats, balance âgée
├── server.py        # outils, ressource et prompt MCP
└── data/invoices.json
tests/
├── test_models.py      # règles métier
├── test_repository.py  # recherche et agrégats
└── test_server.py      # appels d'outils via la couche MCP
```

## Installation

Python 3.11 ou plus récent.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

## Utilisation

### Avec Claude Code

```bash
claude mcp add facturation -- /chemin/absolu/vers/mcp-facturation/.venv/bin/billing-mcp
```

### Avec Claude Desktop

Dans `claude_desktop_config.json` :

```json
{
  "mcpServers": {
    "facturation": {
      "command": "/chemin/absolu/vers/mcp-facturation/.venv/bin/billing-mcp",
      "env": {
        "BILLING_DATA_FILE": "/chemin/absolu/vers/factures.json"
      }
    }
  }
}
```

`BILLING_DATA_FILE` est facultatif : sans lui, le serveur utilise le jeu de démonstration `src/billing_mcp/data/invoices.json`.

### Exemples de questions

- « Liste les factures impayées de SARL Dupont & Fils. »
- « Quel est le reste dû par client au 30 septembre 2026 ? »
- « Montre-moi la balance âgée. »
- « Rédige une relance courtoise pour les impayés de SARL Dupont & Fils. »

## Format des données

Un fichier JSON contenant une liste de factures (ou un objet `{"invoices": [...]}`) :

```json
{
  "id": "FA-2026-002",
  "client": "SARL Dupont & Fils",
  "label": "Développement module stock",
  "issue_date": "2026-02-03",
  "due_date": "2026-03-05",
  "amount_ht": "8400.00",
  "vat_rate": "0.20",
  "paid_amount": "0.00",
  "status": "sent"
}
```

`status` vaut `draft`, `sent`, `paid` ou `cancelled`. `vat_rate` (0.20 par défaut), `paid_amount`, `currency` (EUR par défaut) et `label` sont facultatifs.

## Tests

```bash
pytest
```

101 tests : règles métier, dépôt, et outils appelés de bout en bout via la couche MCP (noms, schémas, sortie structurée, gestion d'erreur).
