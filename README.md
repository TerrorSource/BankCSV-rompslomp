# ICS creditcard → Rompslomp

Kleine self-hosted webapp die transactie-exports (.csv) van
[icscards.nl](https://www.icscards.nl) (ICS creditcard) importeert als
af/bij-schrijvingen in [Rompslomp](https://rompslomp.nl) via de
[Rompslomp API](https://app.rompslomp.nl/developer/endpoints).

Handmatig overtikken of Excel-templates invullen is daarmee niet meer nodig:
CSV uploaden, controleren, importeren.

## Functies

- **Upload & preview**: upload de ICS-export en zie eerst wat er geïmporteerd
  gaat worden voordat er iets in je administratie belandt.
- **Duplicaatdetectie**: transacties die al in Rompslomp staan worden herkend
  (zelfde absolute bedrag op de gekozen rekening, datum binnen ±3 dagen van de
  transactie- of boekingsdatum) en standaard overgeslagen. Handig als je eerder
  al handmatig of via de bankkoppeling hebt geboekt.
- **Veldmapping**: de ICS-"Omschrijving" wordt de beschrijving in Rompslomp, de
  transactiedatum wordt de boekingsdatum, bedragen worden ongewijzigd overgenomen.
- **Instellingen blijven bewaard**: API-token, bedrijf en rekening worden éénmalig
  ingesteld en opgeslagen in een Docker-volume.

## Vereisten

- Docker (lokaal, of een NAS met bijv. Portainer)
- Een Rompslomp-account met **betaald abonnement** (de API is niet beschikbaar op
  het gratis abonnement)
- Een Rompslomp API-token met minimaal de rechten `read:accounts` en
  `manage:payments` (aan te maken in Rompslomp onder Instellingen → API-token)

## Installatie

### Met het kant-en-klare image (aanbevolen)

Er wordt automatisch een multi-arch image (amd64/arm64) gepubliceerd naar de
GitHub Container Registry. Plak de inhoud van
[`docker-compose.yml`](docker-compose.yml) in Portainer
(Stacks → Add stack → Web editor → Deploy), of draai hem met Docker Compose:

```yaml
version: "3"

services:
  ics-rompslomp:
    container_name: ics-rompslomp
    image: ghcr.io/terrorsource/ics-rompslomp:latest
    init: true
    restart: unless-stopped
    network_mode: bridge
    environment:
      - TZ=Europe/Amsterdam
    ports:
      - 8321:8000
    volumes:
      - /share/CACHEDEV1_DATA/Docker/ics-rompslomp:/data
```

Pas het volume-pad links van de `:` aan naar een map op je eigen systeem (het
voorbeeld is een QNAP-pad); daar wordt de configuratie bewaard. Open daarna
`http://<nas-ip>:8321`.

### Zelf bouwen vanaf de broncode

```bash
git clone https://github.com/TerrorSource/ICS-rompslomp.git
cd ICS-rompslomp
docker build -t ics-rompslomp .
docker run -d --name ics-rompslomp -p 8321:8000 \
  -v ./data:/data --restart unless-stopped ics-rompslomp
```

## Gebruik

1. Vul je Rompslomp API-token in en klik **Verbinden & bedrijven ophalen**.
2. Kies je bedrijf en de (creditcard)rekening waarop de transacties geboekt
   moeten worden, en klik **Instellingen opslaan**.
3. Log in op icscards.nl en download de transacties als CSV.
4. Upload het bestand, klik **Controleren** en bekijk de preview: nieuwe regels
   staan aangevinkt, duplicaten zijn gemarkeerd en uitgevinkt.
5. Klik **Geselecteerde regels importeren** — klaar.

## Beveiliging

- Het API-token wordt opgeslagen in `/data/config.json` binnen het Docker-volume
  en verlaat je eigen server niet.
- De webapp heeft **geen eigen loginscherm**: draai hem alleen binnen je eigen
  (thuis)netwerk en zet de poort niet open naar internet.

## Licentie

[MIT](LICENSE)
