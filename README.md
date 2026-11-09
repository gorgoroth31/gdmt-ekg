# gdmt-ekg

Projektarbeit im Modul 'Grundlagen der Medizintechnik' über die Messung und Auswertung von EKG-Daten während eines Videospieles um bestimmte Spielereignisse in der Herzfrequenz zu erkennen. Bestimmte Spiele, die vor allem auf Kämpfe gegen Gegner mit komplizierten Mechaniken ausgelegt sind, erfordern ein hohes Maß an Konzentration und einem Anstieg der Herzfrequenz [(1)](#quellen). Diese Arbeit untersucht weitergehend, ob bestimmte Ereignisse, wie z.B. Tod des Spielers, Pausierung des Kampfes oder Tod des Gegners in dem Spiel 'Hollow Knight' einen Einfluss auf die Herzfrequenz haben können.

# Vorbereitung

Mit einem EKG-Gerät soll die Herzfrequenz während eines Videospiels gemessen und mit einem Python-Skript ausgewertet und dargestellt werden. Der verwendete Kampf wird der 'Trial of the Warrior' sein, bei dem in 12 Wellen verschiedene Gegner erscheinen. Dieser Kampf eignet sich besonders gut, um ein hohes Stresslevel zu erhalten, da die Gegner immer schwerer werden, beziehungsweise die Arena, in der der Kampf ausgetragen wird sich verändert. Parallel zur Messung wird der Kampf aufgezeichnet um die Ereignisse in Relation zur Herzfrequenz zu setzen.

## Einschränkungen

- keine Einnahme von Koffein 8 Stunden vor der Messung, damit Koffein nicht auf die Herzfrequenz einwirken kann

# Protokoll

## Vorbereitung

- verwendetes Gerät: Creative Medical PC-80B
- EKG-Version: SW:V5.6.0.2
- verwendete Elektroden: Unilect (TM) - Various Ag/Ag CI ECG electrodes  
- Stoppuhr bereitlegen
- Videoaufnahmeprogramm starten und vorbereiten
- Hollow Knight starten und zum 'Colosseum of Fools' navigieren

### Anlegen der Elektroden

1. Haut rasieren
2. Haut reiben, um abgestorbene Hautzellen zu entfernen
3. Haut mit Seife waschen
4. Haut gründlich abtrocknen
5. Elektroden anlegen (siehe img/3kanal-ekg.png)

- rot: rechte Schulter
- gelb: linke Schulter
- grün: linke Leiste

## Durchführung

- Start der Messung/Aufzeichnung
- 3 Min Ruhe, keine Bewegung im Spiel
- Start des Kampfes:
  - bei Bedarf Pausierung
  - Aufzeichnung bis der Kampf gewonnen ist
- 3 Minuten Ruhe

## Nachbereitung

- Auslesen der Daten: EKG an Laptop anbinden und Daten aus dem Speicher herauskopieren
- Backup der Messdaten

# Ergebnisse

## Ereignisse im Spiel

- 3 min: Kampfbeginn -> Aufregung ist zu erkennen
- 8 min: erster Tod
- 13 min: zweiter Tod
- 20:50: Sieg
- Darstellung in selbst geschriebener (AI-assisted) Anwendung, die Video abspielen lässt und parallel dazu die Herzfrequenz darstellt.
- Anwendung soll mit docker compose ausführbar sein
- gleitender Mittelwert über kleine Fenster (30 sek -> ermitteln bis typische Schwankung)
- in literatur nachlesen, wie Herzfrequenz ermittelt werden soll über die Zeit
- zuerst ausprobieren, wie die Auswertung passieren soll
- statistischer Test sollte im idealfall nur einmal stattfinden (in Forschungsarbeit)
- Linie zeigt 5 sek früher und später
- auch ableitung?

## Anwendung (EKG-Player)

Web-Anwendung, die eine Videoaufzeichnung des Spiels und die parallel aufgenommene
EKG-Messung synchron abspielt, um Spielereignisse (Kampf, Tod des Spielers, Sieg)
mit der Herzfrequenz in Beziehung zu setzen.

### Starten

```bash
docker compose up
```

Danach im Browser öffnen: <http://localhost:8000>

- Videos im Ordner `videos/` ablegen (MP4/WebM werden vom Browser unterstützt).
- EKG-Rohdaten liegen in `beispieldaten/` (beide Ordner sind im Container unter `/data` eingebunden).

### Bedienung

1. **Video hochladen**: Datei per Drag & Drop auf die Upload-Zone ziehen oder Zone anklicken (MP4/WebM).
2. **EKG-Daten hochladen**: nummerierte SCP-Dateien per Drag & Drop hochladen (Mehrfachauswahl). Die `README.TXT` des Geräts ist **optional** und kann mit hochgeladen oder später separat nachgereicht werden. Die Uploads landen in einer **Session** unter `uploads/`: Dateien können auch in mehreren Schritten hochgeladen werden (solange keine SCP-Dateien vorliegen, erscheint ein Hinweis und die Messungsliste aktualisiert sich nach jedem Upload). Mit `README.TXT` werden die einer Messung zugehörigen Dateien anhand der \"From–To\"-Bereiche ermittelt; ohne `README.TXT` bilden alle SCP-Dateien der Session gemeinsam eine Messung. Hochgeladene Dateien werden nach 24 Stunden automatisch bereinigt.
3. **Wiedergabe starten**: Das Video läuft mit normalen Player-Controls; darunter läuft synchron die EKG-Auswertung.

Mit **Cache löschen** werden alle in dieser Session hochgeladenen Dateien serverseitig entfernt und die Auswahl zurückgesetzt.

Darstellungs-Modi (Tabs unter dem Video):

- **EKG-Kurve**: klassische Kurvenanzeige (P-Welle, QRS-Komplex, T-Welle) als mitlaufendes Fenster um die aktuelle Position; Fensterbreite einstellbar (Standard 10 s).
- **Herzfrequenz**: momentane Herzfrequenz (aus den RR-Intervallen) sowie gleitender Mittelwert über ein einstellbares Fenster (0–120 s, Standard 10 s). Führungslinien markieren ±5 s um die aktuelle Position.
- **HR-Anstieg (Ableitung)**: zeitliche Ableitung der geglätteten Herzfrequenz (BPM pro Minute) – Anstiege grün, Abfälle rot – um phasenweise Frequenzanstiege (z. B. Start eines Kampfes) zu erkennen.

Klick in eine der Zeitleisten spult das Video an die entsprechende Stelle.
Unter **Synchronisation** lässt sich ein Versatz (Sekunden) zwischen Video und
Messung einstellen, um beide Aufzeichnungen zu kalibrieren. Der Versatz gilt als
`EKG-Zeit = Video-Zeit + Versatz`.

Deep-Link für Wiederholbarkeit, z. B.:

```
http://localhost:8000/?video=videos/kampf.mp4&ecg=beispieldaten&record=1&offset=0
```

### Technik

- `app/scp_reader.py` – Parser für das SCP-Format des *Easy ECG Monitor*
  (Creative Medical PC-80B): Sektionsverzeichnis ab dem `SCPECG`-Magic, Sektion 6
  enthält die Waveform (12-Bit-Amplitude, u16-Samples; das hohe Nibble kodiert
  Geraete-R-Zacken-Marken). Die Dateien einer Messung werden anhand der
  `README.TXT` verkettet (Samples laufen kontinuierlich fort).
- `app/analysis.py` – Pan-Tompkins-Algorithmus (QRS-Detektion): Bandpass
  ca. 5–15 Hz (Butterworth-Biquads), Ableitung, Quadrierung, Moving-Window-
  Integration, adaptive Schwellwerte mit Refraktärzeit und Searchback; danach
  Herzfrequenz aus den RR-Intervallen. Validierung gegen die Geräte-Marken der
  Beispieldaten: 94 % Übereinstimmung, mittlere Lageabweichung ca. 1 Sample.
- `app/main.py` – FastAPI-Backend: Upload-Endpoints (Dateien landen unter
  `/data/uploads`, Bereinigung nach 24 Stunden, Löschen per Cache-Button),
  Messungs-Erkennung,
  Analyse-Endpoint, Video-Streaming mit HTTP-Range (Spulen), statisches Frontend
  (Dark Mode, abgerundete Ecken) in `app/static/`.

### Annahmen

- Die Abtastrate des Geräts beträgt 160 Hz ("vermutlich" laut Herstellerangabe;
  die Beispieldaten ergeben damit eine plausible Ruheherzfrequenz von ca. 67 bpm).
  Der Wert liegt als Modulkonstante `DEFAULT_FS` in `app/scp_reader.py` und kann
  über den API-Parameter `fs` übersteuert werden.
- Die Amplitude ist unkalibriert (12-Bit-AD-Wert, Nulllinie v. a. ~2048);
  angezeigt wird in ADC-Einheiten.

# Diskussion

# Quellen

- [1] : ValladÃo SP, Middleton J, Andre TL. Esport: Fortnite Acutely Increases Heart Rate of Young Men. Int J Exerc Sci. 2020 Sep 1;13(6):1217-1227. doi: 10.70252/ZXZG4481. PMID: 33042380; PMCID: PMC7523899.
