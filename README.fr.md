# ecoflow-nut-bridge

*[🇬🇧 English](README.md) · 🇫🇷 Français*

Expose une station d'énergie portable **EcoFlow DELTA 3** comme un onduleur
**NUT (Network UPS Tools)** standard, via Bluetooth Low Energy. Le pont interroge
la DELTA 3 en BLE, traduit sa télémétrie (niveau de charge, watts d'entrée/sortie
AC, présence de l'alimentation secteur) en variables NUT, écrit un fichier d'état
`dummy-ups` et lance `upsd` sur le port 4141 — n'importe quel client NUT (client
intégré Unraid, Synology, `upsc`, …) peut alors surveiller la DELTA 3 comme un
onduleur classique.

> ⚠️ **Avertissement.** Ce projet **n'est ni affilié, ni autorisé, ni approuvé
> par EcoFlow**. Il utilise un protocole BLE non documenté, reconstitué par la
> communauté (voir [Crédits](#10-crédits)). Utilisation à vos risques et périls.
> Il **n'utilise pas** l'API cloud d'EcoFlow pour la télémétrie ou le contrôle.

---

## Sommaire

1. [Ce que ça fait](#1-ce-que-ça-fait)
2. [Avertissement](#2-avertissement)
3. [Matériel supporté](#3-matériel-supporté)
4. [Démarrage rapide (Docker sur Unraid)](#4-démarrage-rapide-docker-sur-unraid)
5. [Déploiement en production (Pi Zero 2W)](#5-déploiement-en-production-raspberry-pi-zero-2w)
6. [Référence de configuration](#6-référence-de-configuration)
7. [Configuration du client NUT](#7-configuration-du-client-nut)
8. [Dépannage](#8-dépannage)
9. [Architecture](#9-architecture)
10. [Crédits](#10-crédits)

---

## 1. Ce que ça fait

Un seul démon asynchrone se connecte à la DELTA 3 en BLE, lit son état toutes les
quelques secondes, en déduit les variables NUT (`ups.status` / `battery.charge` /
`ups.load` / autonomie) et tient à jour un fichier `.dev` `dummy-ups`. Le pilote
`dummy-ups` de NUT relit ce fichier et `upsd` le sert sur le port 4141. Le même
code Python tourne tel quel en conteneur Docker (validation) et en service
systemd bare-metal (production) ; seul l'enrobage diffère.

Il expose aussi des commandes manuelles — basculer les sorties **AC**, **USB** et
**12V DC** — sous forme de fonctions Python et d'une petite CLI. Une logique
**d'arrêt automatique** optionnelle peut couper la sortie AC quand la batterie est
critique (voir [Arrêt automatique](#arrêt-automatique)) ; elle est désactivée par
défaut.

## 2. Avertissement

Voir l'encadré plus haut. Le protocole BLE est rétro-conçu, peut changer avec les
mises à jour du firmware, et est implémenté au mieux. **La justesse de la lecture
(niveau de charge + état secteur) est prioritaire sur l'exhaustivité des
fonctionnalités.**

## 3. Matériel supporté

| Élément | Détail |
|---------|--------|
| Appareil confirmé | EcoFlow **DELTA 3** (1024 Wh, 1800 W AC), préfixe de série `P231`, nom BLE `EF-D3` |
| Famille de protocole | `pd335` (protocole BLE moderne, chiffré, en protobuf) |
| Hôte de test | Unraid + dongle USB BT Realtek RTL8821CU (BlueZ) |
| Hôte de production | Raspberry Pi Zero 2W, Raspberry Pi OS Lite 64 bits, BT intégré |

D'autres modèles de la même famille (DELTA 3 Plus/Max, River 3, …) utilisent le
même cadrage et les mêmes numéros de champs protobuf et fonctionneront sans doute
avec une config adaptée, mais seule la DELTA 3 est ciblée ici.

> ### Note protocole — DELTA 3 ≠ DELTA 2
> La DELTA **2** utilise un ancien protocole BLE *en clair* à offsets fixes. La
> DELTA **3** utilise le nouveau protocole **chiffré, basé sur protobuf**
> (messages `DisplayPropertyUpload` / `ConfigWrite` dans une trame V3 avec CRC8 +
> CRC16 et une charge utile désobfusquée par XOR). Ce pont implémente le
> protocole **DELTA 3**. Le chemin de lecture/décodage est testé unitairement
> contre de **vraies trames capturées** d'un appareil cousin partageant les mêmes
> numéros de champs protobuf.

> ### Authentification
> La DELTA 3 négocie une session chiffrée (`encrypt_type 7`, ECDH). L'étape finale
> d'authentification hache `md5(user_id + serial)`, où `user_id` est l'identifiant
> de votre compte EcoFlow. Cet identifiant sert **une seule fois, localement, à
> dériver le secret de session BLE** — aucun trafic de télémétrie ou de contrôle
> ne passe par le cloud. Récupérez-le une fois (via l'API de connexion EcoFlow ou
> les diagnostics de l'app) et placez-le dans `ecoflow.user_id`. Si votre unité
> annonce `encrypt_type 0` ou `1`, aucun `user_id` n'est nécessaire. Voir
> [Dépannage](#8-dépannage).

## 4. Démarrage rapide (Docker sur Unraid)

Le conteneur embarque BlueZ, le serveur NUT et le démon du pont.

1. Créez une config à partir de l'exemple :

   ```bash
   mkdir -p config
   cp config/config.example.yaml config/config.yaml
   # éditez config/config.yaml : mac, serial, et user_id (si nécessaire)
   ```

2. Utilisez le fichier compose fourni (ajustez le propriétaire de l'image / le
   fuseau horaire), puis :

   ```bash
   docker compose up -d
   docker compose logs -f
   ```

   Il tourne avec `network_mode: host` (pour que le conteneur voie l'adaptateur
   Bluetooth du noyau et qu'`upsd` soit joignable sur `<hôte>:4141`) et
   `privileged: true`. Le conteneur lance son propre `bluetoothd`, donc aucun
   montage D-Bus de l'hôte n'est nécessaire.

3. Vérifiez :

   ```bash
   docker exec ecoflow-nut-bridge upsc ecoflow@localhost:4141
   ```

   Vous devriez voir des valeurs cohérentes : `battery.charge`, `ups.status`,
   `ups.load`, etc.

Les images sont construites et publiées automatiquement sur
`ghcr.io/<propriétaire>/ecoflow2nut-pibridge` pour `linux/amd64` et `linux/arm64`
à chaque push sur `main`.

> Sur un hôte qui possède déjà un `bluetoothd` (par ex. un Raspberry Pi), faites
> en sorte que le conteneur réutilise le Bluetooth de l'hôte avec
> `-e ECOFLOW_USE_HOST_DBUS=1 -v /run/dbus:/run/dbus`, sinon les deux `bluetoothd`
> se disputeront l'adaptateur `hci0`.

## 5. Déploiement en production (Raspberry Pi Zero 2W)

systemd bare-metal, sans Docker. Depuis une copie du dépôt sur le Pi :

```bash
sudo ./systemd/install.sh
```

L'installeur :

* installe `bluez`, `nut-server`, `nut-client`, `python3-venv` ;
* crée l'utilisateur de service `ecoflow` (dans les groupes `bluetooth` et `nut`) ;
* construit un venv dans `/opt/ecoflow-nut-bridge/.venv` et installe le paquet ;
* dépose la config NUT dans `/etc/nut/` et règle `MODE=netserver` ;
* installe la config dans `/etc/ecoflow-nut/config.yaml` ;
* installe et active le service systemd `ecoflow-nut-bridge.service` ;
* ajoute un drop-in pour que `nut-server` démarre **après** le pont. Le pont,
  via `ExecStartPre`, amorce d'abord le fichier d'état `dummy-ups` (sinon le
  pilote échoue au démarrage à froid) et **débloque/active le Bluetooth** (rfkill
  + mise sous tension de l'adaptateur), pour être sûr de boot.

Ensuite :

```bash
sudo nano /etc/ecoflow-nut/config.yaml   # MAC / série / user_id / auto_shutdown
sudo nano /etc/nut/upsd.users            # définissez de vrais mots de passe
sudo systemctl start ecoflow-nut-bridge  # amorce le fichier d'état, puis connecte
sudo systemctl restart nut-server        # démarre upsd + le pilote dummy-ups
upsc ecoflow@localhost:4141
```

Raspberry Pi OS lance `bluetoothd` par défaut, donc le BLE fonctionne sans
configuration supplémentaire. Suivez la progression avec `journalctl -u
ecoflow-nut-bridge -f` — cherchez `ble.authenticated` puis `state.updated`.

> Le Pi est alimenté par le port USB-A de la DELTA 3 ; gardez donc
> `auto_shutdown.cut_usb` à sa valeur par défaut `false` — couper l'USB tuerait
> le pont lui-même.

## 6. Référence de configuration

Exemple annoté complet : [`config/config.example.yaml`](config/config.example.yaml).

| Clé | Défaut | Signification |
|-----|--------|---------------|
| `ecoflow.mac` | — (requis) | Adresse MAC BLE de la DELTA 3 |
| `ecoflow.serial` | — | Numéro de série (utilisé pour l'auth + remonté à NUT) |
| `ecoflow.poll_interval_seconds` | `5` | Fréquence de vérification du lien BLE — un chien de garde, **pas** une fréquence d'échantillonnage. La DELTA 3 émet sa télémétrie à son propre rythme ; c'est `sqlite.min_interval_seconds` qui détermine la finesse des graphiques |
| `ecoflow.encrypt_type` | `auto` | `auto` lit le type dans l'annonce BLE ; ou forcez `0`/`1`/`7` |
| `ecoflow.user_id` | `""` | Identifiant de compte EcoFlow, requis pour `encrypt_type 7` |
| `ble.adapter` | `hci0` | Adaptateur BlueZ |
| `ble.connect_timeout_seconds` | `30` | Délai de connexion BLE |
| `ble.reconnect_backoff_max_seconds` | `60` | Backoff exponentiel max de reconnexion |
| `nut.dev_file_path` | `/var/run/nut/ecoflow.dev` | Fichier d'état dummy-ups (doit correspondre à `ups.conf`) |
| `nut.battery_capacity_wh` | `1024` | Capacité du pack pour l'estimation d'autonomie |
| `nut.thresholds.low_battery_percent` | `25` | En dessous → `OB LB` (déclenche l'arrêt des clients) |
| `nut.thresholds.critical_battery_percent` | `10` | Informatif ; l'auto-cut utilise `auto_shutdown.trigger_soc_percent` |
| `nut.static_values.*` | — | Valeurs de plaque remontées telles quelles (tension, fréquence, fabricant, modèle, série) |
| `logging.level` / `logging.format` | `INFO` / `json` | Niveau structlog et sortie `json`/`console` |
| `control_socket_path` | `/var/run/nut/ecoflow-nut.sock` | Socket local pour envoyer des commandes via le démon en cours |
| `auto_shutdown.enabled` | `false` | Interrupteur principal de l'auto-coupure (opt-in) |
| `auto_shutdown.trigger_soc_percent` | `10` | Arme + coupe à/sous ce SoC, sur batterie uniquement |
| `auto_shutdown.recover_soc_percent` | `15` | Désarme quand le SoC remonte à ça (ou au retour du secteur) |
| `auto_shutdown.grace_period_seconds` | `300` | Délai après armement (déclencheur SoC) avant coupure |
| `auto_shutdown.min_load_watts` | `null` | Déclencheur faible charge : coupe si la sortie AC reste ≤ ça (sur batterie, tout SoC). `null` = désactivé |
| `auto_shutdown.load_grace_seconds` | `60` | Anti-rebond du déclencheur faible charge |
| `auto_shutdown.cut_ac` / `cut_usb` / `cut_dc` | `true`/`false`/`false` | Sorties à couper |
| `auto_shutdown.restore_on_recovery` | `false` | Rallumer les sorties coupées au retour du courant/SoC |

### Correspondance des variables NUT

| Variable NUT | Source |
|--------------|--------|
| `ups.status` | `OL` si secteur présent et puissance > `ac_input_present_min_watts` ; `OB LB` si SoC < seuil bas ; sinon `OB` |
| `battery.charge` | `cms_batt_soc` (SoC %) |
| `battery.runtime` | `(SoC/100 · capacité_wh · 0,9) / watts_sortie_AC · 3600`, ou `99999` au repos |
| `ups.realpower` | Watts de sortie AC (`pow_get_ac_out`, valeur absolue) |
| `ups.load` | Sortie AC en pourcentage de `ups.realpower.nominal` |
| `ups.realpower.nominal` | `1800` |
| `input.*` / `output.*` | Valeurs de plaque statiques |

### CLI

```bash
ecoflow-nut --config config.yaml read     # connecte, lit une trame, affiche en JSON
ecoflow-nut --config config.yaml run      # lance le démon (mode par défaut)
ecoflow-nut --config config.yaml ac on    # bascule la sortie AC  (aussi : ac off)
ecoflow-nut --config config.yaml usb on   # bascule la sortie USB (aussi : usb off)
ecoflow-nut --config config.yaml dc on    # bascule le 12V DC     (aussi : dc off)
```

La DELTA 3 n'accepte **qu'une seule** connexion BLE à la fois ; les commandes
`ac`/`usb`/`dc` parlent donc au **démon en cours** via un socket de contrôle local
(`control_socket_path`) et c'est lui qui envoie la commande sur sa connexion
existante — pas besoin d'arrêter le pont. Si aucun démon ne tourne, la CLI bascule
en connexion directe.

```bash
# bare metal (Pi)
sudo -u ecoflow /opt/ecoflow-nut-bridge/.venv/bin/ecoflow-nut \
  --config /etc/ecoflow-nut/config.yaml ac off

# docker
docker exec ecoflow-nut-bridge ecoflow-nut --config /app/config/config.yaml ac off
```

### Arrêt automatique

Désactivé par défaut. Quand `auto_shutdown.enabled` est vrai, **deux déclencheurs
indépendants** (l'un ou l'autre, et toujours sur batterie) peuvent armer une
coupure :

- **Déclencheur SoC** — le SoC tombe à `trigger_soc_percent`, puis après
  `grace_period_seconds` (le temps que vos clients NUT s'éteignent sur le statut
  `OB LB`) il envoie `set_ac_enabled(false)` une fois. Ne se réarme qu'après
  rétablissement ; une remontée à `recover_soc_percent` le désarme.
- **Déclencheur faible charge** — la sortie AC reste ≤ `min_load_watts` pendant
  `load_grace_seconds`, à **n'importe quel** SoC. Détecte que « l'équipement
  protégé s'est éteint, il n'y a plus rien à alimenter » et coupe l'onduleur au
  ralenti pour préserver la batterie. Une charge au-dessus du seuil réarme
  l'anti-rebond. Désactivé tant que `min_load_watts` n'est pas défini.

`cut_usb`/`cut_dc` existent mais sont à `false` par défaut ; **n'activez jamais
`cut_usb` si l'hôte du pont est alimenté par le port USB de la DELTA 3.**

Ceci complète — sans le remplacer — le comportement NUT normal : les clients
s'éteignent d'eux-mêmes sur `ups.status` (`OB LB`) ; l'arrêt automatique coupe en
plus la sortie une fois qu'ils sont éteints. Au retour du secteur, avec
`restore_on_recovery: true`, la sortie AC est rallumée — vos équipements
redémarrent (s'ils sont réglés sur « rallumer au retour du secteur » dans leur
BIOS).

## 7. Configuration du client NUT

### Unraid (client NUT intégré)

Settings → **UPS Settings** :

* Type / mode : **Network UPS Tools — remote** (ou « Custom »).
* IP du serveur NUT distant : l'hôte du pont.
* Nom de l'UPS : `ecoflow`, port `4141`.
* Identifiants : ceux de `monuser` dans `upsd.users`.

### Synology DSM

Panneau de configuration → **Matériel et alimentation → Onduleur** :

* Activez la prise en charge → **Serveur UPS réseau**.
* IP du serveur UPS réseau : l'hôte du pont.
* (Synology suppose le nom d'UPS `ups` et le port 3493 ; ajoutez si besoin une
  section alias `[ups]` dans `ups.conf` pointant vers le même fichier `.dev`, ou
  renommez `[ecoflow]`.)

### N'importe quel hôte avec `upsc`

```bash
upsc ecoflow@<hôte-du-pont>:4141
upsc ecoflow@<hôte-du-pont>:4141 battery.charge
```

## 8. Dépannage

**Une seule connexion BLE à la fois.** La DELTA 3 n'accepte qu'un seul client
Bluetooth. Si une **app EcoFlow sur téléphone**, une intégration **Home Assistant
(`ef_ble`)** ou un **autre conteneur** est connecté, l'unité **n'émet plus
d'annonce** et le pont la signalera comme « not found during scan ». Fermez/
désactivez les autres clients ; il ne doit y avoir qu'un seul propriétaire.

**`encrypt_type 7 (ECDH) requires 'ecoflow.user_id'`.** Votre unité utilise la
poignée de main chiffrée. Renseignez `ecoflow.user_id` (voir
[Authentification](#authentification)). Pour voir le type annoncé, lancez
`ecoflow-nut --config config.yaml read` avec `logging.format: console` et
regardez `encrypt_type` dans la ligne `ble.found`.

**Bluetooth bloqué au démarrage (`Resource Not Ready` / `org.bluez.Error.Failed`).**
L'adaptateur est probablement *soft-blocked* par rfkill ou non alimenté.
Vérifiez `rfkill list bluetooth` et `bluetoothctl show | grep Powered`. Le service
systemd se ré-soigne tout seul (il fait `rfkill unblock` + `bluetoothctl power on`
au démarrage) ; en manuel : `sudo rfkill unblock bluetooth && sudo bluetoothctl
power on`.

**Dongle Realtek RTL8821CU (Unraid).** Le firmware/pilote doit être chargé par le
**noyau de l'hôte** — vérifiez avec `dmesg | grep -i bluetooth` que `hci0` est
bien enregistré. Le conteneur embarque et lance son propre `bluetoothd`, donc
l'hôte n'a pas besoin de BlueZ en espace utilisateur. En revanche le noyau
n'expose l'adaptateur que dans l'espace de noms réseau de l'hôte : le conteneur
doit donc tourner en **networking hôte**.

**Déconnexions BLE fréquentes.** Normal — le pont se reconnecte avec un backoff
exponentiel (jusqu'à `reconnect_backoff_max_seconds`). Si aucune lecture réussie
n'a lieu pendant 2 minutes, un *watchdog* termine le processus pour que
systemd/Docker le relance proprement.

**`upsc` renvoie « Connection refused » ou « Driver not connected ».** `upsd`
n'est pas lancé ou le pilote `dummy-ups` ne peut pas lire le fichier `.dev`.
Vérifiez que `nut.conf` est en `MODE=netserver`, que `ups.conf` contient bien la
section `[ecoflow]` et que son `port` correspond à `nut.dev_file_path`, puis
`sudo systemctl restart nut-server`. Le diagnostic précis : `sudo upsd -D`.

**Valeurs figées ou partielles.** Chaque trame BLE ne transporte que les champs
*modifiés* ; le pont les accumule. Le SoC et l'état AC apparaissent quelques
secondes après la connexion.

## 9. Architecture

```
                          ┌──────────────── hôte du pont (Pi / Unraid) ──────────┐
   EcoFlow DELTA 3        │                                                       │
  ┌───────────────┐  BLE  │  ┌────────────────────┐   écrit      ┌─────────────┐ │
  │  firmware     │◀─────▶│  │  démon ecoflow-nut  │ ───────────▶ │ ecoflow.dev │ │
  │  pd335        │ GATT  │  │  • transport bleak  │  (format     │ fichier état│ │
  │ DisplayProp.  │ 0002/ │  │  • trame V3 + CRC   │   dummy-ups) └──────┬──────┘ │
  │ ConfigWrite   │ 0003  │  │  • décodage protobuf│                     │ lit    │
  └───────────────┘       │  │  • traduction NUT   │              ┌──────▼──────┐ │
                          │  └────────────────────┘              │ pilote      │ │
                          │                                       │ dummy-ups   │ │
                          │                                       └──────┬──────┘ │
                          │                                  ┌───────────▼──────┐ │
   Clients NUT  ◀─────────┼──────────  TCP :4141  ──────────│       upsd        │ │
  (Unraid, Synology,      │                                  └──────────────────┘ │
   upsc, …)               └───────────────────────────────────────────────────────┘
```

## 10. Crédits

Le protocole BLE d'EcoFlow n'est pas documenté. Cette implémentation s'appuie sur
le travail de rétro-ingénierie de la communauté — en particulier
[rabits/ha-ef-ble](https://github.com/rabits/ha-ef-ble) (protocole chiffré
moderne, numéros de champs protobuf, poignée de main) et
[vwt12eh8/hassio-ecoflow](https://github.com/vwt12eh8/hassio-ecoflow) (cadrage et
CRC), avec recoupements via
[tolwi/hassio-ecoflow-cloud](https://github.com/tolwi/hassio-ecoflow-cloud),
[nielsole/ecoflow-bt-reverse-engineering](https://github.com/nielsole/ecoflow-bt-reverse-engineering)
et `anton-ptashnik/ecoflow-api-py`. Voir [NOTICE](NOTICE) pour les licences du
matériel intégré. Construit avec [bleak](https://github.com/hbldh/bleak) et
[Network UPS Tools](https://networkupstools.org/).

Sous licence [MIT](LICENSE).
