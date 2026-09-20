# 🪟 Passerelle AM43 vers MQTT pour Home Assistant

Passerelle Python containerisée haute performance permettant de piloter des moteurs de stores enrouleurs **AM43** via **Bluetooth Low Energy (BLE)** et de les exposer sur un broker **MQTT** avec découverte automatique pour **Home Assistant (MQTT Discovery)**.

---

## ✨ Fonctionnalités

- 📡 **Découverte automatique Home Assistant (MQTT Discovery) :** Création instantanée des entités `cover` (volet/store avec slider de position, Ouvrir, Fermer, Stop) et `sensor` (niveau de batterie en %).
- 🛡️ **File d'attente asynchrone (`asyncio.Queue`) :** Sérialisation automatique des commandes BLE pour éviter toute collision ou blocage du contrôleur Bluetooth hôte.
- 🔄 **Reconnexion automatique & Résilience :** Gestion des déconnexions intempestives BLE et reconnexion automatique au broker MQTT avec état de disponibilité LWT (*Last Will and Testament*).
- 🔋 **Sondage périodique de la batterie :** Requête automatique du statut batterie toutes les 4 heures (configurable).
- 🐳 **Conteneurisé avec Docker :** Déploiement en une commande avec montage D-Bus sécurisé (`/var/run/dbus`).
- ⚙️ **Configuration flexible :** Fichier `config.yaml` ou variables d'environnement.

---

## 🏗️ Architecture du projet

```text
am43-mqtt-gateway/
├── .github/
│   └── workflows/
│       └── deploy.yml          # Pipeline CI/CD GitHub Actions (tests & build Docker)
├── src/
│   ├── __init__.py
│   ├── am43.py                 # Protocole AM43 (calcul checksum XOR, trames HEX, décodage)
│   ├── ble_worker.py           # File asynchrone Bleak sérialisant les accès Bluetooth
│   ├── mqtt_client.py          # Client MQTT paho-mqtt v2, discovery et publication d'états
│   └── main.py                 # Orchestration asyncio, configuration et signaux POSIX
├── tests/
│   └── test_am43.py            # Tests unitaires du protocole AM43
├── config.example.yaml         # Modèle de configuration
├── config.yaml                 # Configuration locale (ignorée par Git)
├── requirements.txt            # Dépendances Python (bleak, paho-mqtt, pyyaml)
├── Dockerfile                  # Image Python 3.12-slim avec BlueZ et D-Bus
├── docker-compose.yml          # Déploiement Docker en mode host
└── README.md
```

---

## 📋 Prérequis matériels et système

1. **Machine hôte Linux** équipée d'un adaptateur Bluetooth 4.0+ (BLE).
2. **Démon BlueZ et D-Bus** actifs sur l'hôte :
   ```bash
   sudo apt update && sudo apt install -y bluez dbus
   sudo systemctl status bluetooth
   ```
3. **Docker & Docker Compose** installés.

---

## 🚀 Installation et Démarrage rapide

### 1. Configuration (`config.yaml`)

Copiez le fichier d'exemple si ce n'est pas déjà fait :
```bash
cp config.example.yaml config.yaml
```

Éditez `config.yaml` pour renseigner l'adresse IP de votre broker MQTT (souvent l'IP de votre Home Assistant) et les identifiants :

```yaml
mqtt:
  host: "192.168.1.50"          # IP de votre serveur Home Assistant / Mosquitto
  port: 1883
  username: "votre_utilisateur" # Laissez vide si pas d'authentification
  password: "votre_mot_de_passe"
  base_topic: "am43"
  discovery_prefix: "homeassistant"

devices:
  - id: "store_droite"
    name: "Store Droite"
    mac: "02:21:B8:76:F7:2F"

  - id: "store_gauche"
    name: "Store Gauche"
    mac: "02:F6:E1:20:59:C7"

settings:
  connect_timeout: 15.0
  max_retries: 2
  battery_poll_interval_hours: 4
```

---

### 2. Lancement avec Docker Compose (Recommandé)

Construisez et démarrez le conteneur en arrière-plan :

```bash
docker compose up -d --build
```

Visualiser les journaux d'exécution en direct :
```bash
docker compose logs -f
```

Arrêter la passerelle :
```bash
docker compose down
```

---

### 3. Exécution directe en Python (Mode Développement)

Si vous souhaitez tester directement sans Docker :

```bash
# 1. Créer un environnement virtuel
python3 -m venv .venv
source .venv/bin/activate  # Sur Linux

# 2. Installer les dépendances
pip install -r requirements.txt

# 3. Lancer les tests unitaires
python -m unittest discover -s tests

# 4. Démarrer la passerelle
python -m src.main
```

---

## 🏠 Intégration Home Assistant

Grâce au protocole **MQTT Discovery**, aucune configuration YAML n'est requise dans Home Assistant !

Dès le premier démarrage de la passerelle :
1. Rendez-vous dans **Paramètres > Appareils et services > MQTT**.
2. Vous verrez apparaître deux nouveaux appareils : **Store Droite** et **Store Gauche**.
3. Chaque appareil contient :
   - Une entité `cover` : commande d'ouverture, fermeture, stop, et slider de position 0 à 100%.
   - Une entité `sensor` : niveau de batterie (`%`).

> [!NOTE] Convention d'ouverture
> Les moteurs AM43 considèrent `0` = ouvert à 100% et `100` = fermé à 100%. La passerelle configure automatiquement Home Assistant (`position_open: 0`, `position_closed: 100`) afin que vos sliders et boutons agissent dans le sens intuitif dans votre tableau de bord.

---

## 📡 Répertoire des Topics MQTT

| Topic | Direction | Rôle | Payload |
| :--- | :--- | :--- | :--- |
| `am43/status` | Passerelle ➜ Broker | Disponibilité globale (LWT) | `online` / `offline` |
| `am43/<id>/set` | HA ➜ Passerelle | Action directe | `OPEN`, `CLOSE`, `STOP` |
| `am43/<id>/set_position` | HA ➜ Passerelle | Consigne de position | `0` à `100` |
| `am43/<id>/position` | Passerelle ➜ HA | Position actuelle rapportée | `0` à `100` |
| `am43/<id>/state` | Passerelle ➜ HA | État du store | `open`, `closed`, `opening`, `closing`, `stopped` |
| `am43/<id>/battery` | Passerelle ➜ HA | Niveau de batterie | `0` à `100` |
| `am43/<id>/battery/query` | HA ➜ Passerelle | Déclenchement manuel requête batterie | n'importe quelle valeur |

---

## 🔧 Dépannage & Astuces

- **Le conteneur ne trouve pas l'adaptateur Bluetooth :**
  Vérifiez que `/var/run/dbus` est bien monté et que le service bluetooth de la machine hôte est actif (`sudo systemctl restart bluetooth`).
- **Erreurs de connexion BLE fréquentes :**
  Les adaptateurs USB Bluetooth bon marché peuvent parfois saturer. Si nécessaire, réinitialisez l'adaptateur hôte avec `sudo hciconfig hci0 reset` ou `sudo rfkill unblock bluetooth`.
- **Changement d'adresse MAC :**
  Modifiez simplement l'adresse dans `config.yaml` et relancez le conteneur (`docker compose restart`).
