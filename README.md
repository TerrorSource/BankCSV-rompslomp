# ICS creditcard → Rompslomp importer

Simpele webapp (Docker) om de transactie-export (.csv) van icscards.nl te importeren
als af/bij-schrijvingen in [Rompslomp](https://app.rompslomp.nl) via de API.

- Duplicaatdetectie: regels die al in Rompslomp staan (zelfde absolute bedrag op de
  gekozen rekening, datum binnen ±3 dagen van de transactie- of boekingsdatum) worden
  gemarkeerd en overgeslagen.
- De "Omschrijving" uit de ICS-export wordt de "Beschrijving" in Rompslomp,
  "Transactiedatum" wordt de datum. Bedragen worden ongewijzigd overgenomen.
- Instellingen (API-token, bedrijf, rekening) worden bewaard in een volume en
  blijven bestaan na een herstart.

> **Let op:** de Rompslomp API is alleen beschikbaar op betaalde abonnementen.
> Maak een token aan via Rompslomp → Instellingen → API-token, met minimaal de
> rechten `read:accounts` en `manage:payments`.

## Lokaal draaien

```bash
docker compose up -d --build
```

Open daarna http://localhost:8321

## Op je NAS via Portainer

Dit repository bevat een GitHub Actions-workflow die bij elke push naar `main`
automatisch een Docker-image bouwt (amd64 + arm64) en publiceert naar de GitHub
Container Registry: `ghcr.io/terrorsource/ics-rompslomp:latest`.

**Eenmalig na de eerste push:** het image is standaard privé. Maak het publiek via
GitHub → jouw profiel → Packages → `ics-rompslomp` → Package settings →
Change visibility → Public. (Of voeg in Portainer een registry toe met een GitHub
Personal Access Token als je het privé wilt houden.)

Daarna in Portainer:

1. **Stacks** → **Add stack**, geef een naam (bijv. `ics-rompslomp`).
2. Plak de inhoud van [`portainer-stack.yml`](portainer-stack.yml) in de web editor.
3. **Deploy the stack** en open `http://<nas-ip>:8321`.

Nieuwe versie uitrollen: push naar GitHub, wacht tot de Action klaar is, en klik in
Portainer bij de stack op **Update the stack** met "Re-pull image" aangevinkt.

## Eerste gebruik

1. Vul je Rompslomp API-token in en klik **Verbinden & bedrijven ophalen**.
2. Kies je bedrijf en de creditcard-rekening, klik **Instellingen opslaan**.
3. Upload een CSV-export van ICS en klik **Controleren**.
4. Controleer de preview en klik **Geselecteerde regels importeren**.

## Beveiliging

- Je API-token wordt opgeslagen in `/data/config.json` (het `ics_rompslomp_data`
  volume / lokaal `./data`). Dit staat in `.gitignore` en komt dus nooit op GitHub.
- De webapp heeft geen eigen login: draai hem alleen in je eigen (thuis)netwerk en
  zet de poort niet open naar internet.
