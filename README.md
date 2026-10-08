# Bank-CSV → Rompslomp

Kleine self-hosted webapp die bank- en creditcardexports (.csv) importeert als
af/bij-schrijvingen in [Rompslomp](https://rompslomp.nl) via de
[Rompslomp API](https://app.rompslomp.nl/developer/endpoints).

Ondersteunde formaten (automatisch herkend):

- **ICS creditcard** ([icscards.nl](https://www.icscards.nl)) — puntkomma's,
  `dd-mm-jjjj`, Nederlandse bedragen
- **GoDutch** — komma's, ISO-datums, aparte Debit/Credit-kolommen
  (bedrag wordt Credit − Debit: af = negatief, bij = positief)
- **MT940** — generieke parser voor `:61:`/`:86:`-records, inclusief
  gestructureerde SEPA-omschrijvingen (`/NAME/`, `/REMI/`)
- **ICS API-JSON** — het antwoord van het interne transactie-API van
  icscards.nl (zie hieronder)

### Transacties direct bij ICS ophalen

Geïnspireerd op [ics-cards-downloadstatements](https://github.com/sietsevdschoot/ics-cards-downloadstatements):
in plaats van een CSV te downloaden kun je het geauthenticeerde API-request
uit je browser plakken. Log in op icscards.nl en open het
transactie-overzicht. Open DevTools → Network, filter op `transactionsv3`,
rechtsklik op het `transactionsv3?accountNumber=…&fromDate=…&untilDate=…`-request
→ Copy → **Copy as cURL**, en plak dat in de app. De periode en `pageSize`
in de URL kun je na het plakken aanpassen. De app
haalt de transacties dan zelf op en zet ze door dezelfde controle en import.

Volledig automatisch inloggen bij ICS is bewust niet ingebouwd: de bank
gebruikt MFA en anti-bot-maatregelen, en je bankwachtwoord hoort niet in een
containertje thuis. Veiligheidsmaatregelen bij het plakken: alleen requests
naar `icscards.nl` worden uitgevoerd, het geplakte request (met je
sessie-cookie) wordt éénmalig gebruikt en nergens opgeslagen, en de
ICS-sessie zelf vervalt na ±1 minuut inactiviteit.

Handmatig overtikken of Excel-templates invullen is daarmee niet meer nodig:
CSV uploaden, controleren, importeren.

## Functies

- **Meerdere administraties**: stel per administratie een Rompslomp-bedrijf en
  rekening in (bijv. één bedrijf met een ICS-creditcard en een ander bedrijf
  met een GoDutch-rekening, binnen hetzelfde Rompslomp-account).
- **Upload & preview**: zie eerst wat er geïmporteerd gaat worden voordat er
  iets in je administratie belandt.
- **Duplicaatdetectie**: transacties die al in Rompslomp staan worden herkend
  (zelfde absolute bedrag op de gekozen rekening, datum binnen ±3 dagen van de
  transactie- of boekingsdatum) en standaard overgeslagen. Elke bestaande
  boeking dekt maximaal één CSV-regel af, zodat twee identieke transacties op
  dezelfde dag correct worden geteld.
- **Veldmapping**: de omschrijving (bij GoDutch inclusief tegenpartij) wordt de
  beschrijving in Rompslomp, de transactiedatum wordt de boekingsdatum,
  uitgaven worden negatief en ontvangsten positief geboekt (ICS geeft
  uitgaven positief, dus daar wordt het teken omgedraaid).
- **Instellingen blijven bewaard**: API-token en administraties worden éénmalig
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
  bankcsv-rompslomp:
    container_name: bankcsv-rompslomp
    image: ghcr.io/terrorsource/bankcsv-rompslomp:latest
    init: true
    restart: unless-stopped
    network_mode: bridge
    environment:
      - TZ=Europe/Amsterdam
    ports:
      - 8321:8000
    volumes:
      - /share/CACHEDEV1_DATA/Docker/bankcsv-rompslomp:/data
```

Pas het volume-pad links van de `:` aan naar een map op je eigen systeem (het
voorbeeld is een QNAP-pad); daar wordt de configuratie bewaard. Open daarna
`http://<nas-ip>:8321`.

### Zelf bouwen vanaf de broncode

```bash
git clone https://github.com/TerrorSource/BankCSV-rompslomp.git
cd BankCSV-rompslomp
docker build -t bankcsv-rompslomp .
docker run -d --name bankcsv-rompslomp -p 8321:8000 \
  -v ./data:/data --restart unless-stopped bankcsv-rompslomp
```

## Gebruik

1. Vul je Rompslomp API-token in en klik **Verbinden & administraties bewerken**.
2. Maak per bank/creditcard een administratie aan: geef een naam en kies het
   Rompslomp-bedrijf en de rekening waarop de transacties geboekt moeten
   worden. Klik **Instellingen opslaan**.
3. Download de transacties als CSV bij je bank (icscards.nl of GoDutch).
4. Kies de administratie, upload het bestand en klik **Controleren**: nieuwe
   regels staan aangevinkt, duplicaten zijn gemarkeerd en uitgevinkt.
5. Klik **Geselecteerde regels importeren** — klaar.

## Beveiliging

- Het API-token wordt opgeslagen in `/data/config.json` binnen het Docker-volume
  en verlaat je eigen server niet.
- De webapp heeft **geen eigen loginscherm**: draai hem alleen binnen je eigen
  (thuis)netwerk en zet de poort niet open naar internet.

## Licentie

[MIT](LICENSE)
