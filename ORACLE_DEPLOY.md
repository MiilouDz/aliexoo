# Deploying on Oracle Cloud — Always Free (Truly Free, Forever)

This option gives you a real VM with full root access and no network restrictions — perfectly suitable for Telethon (raw TCP connections to Telegram) and continuous 24/7 operation, at no cost as long as you stay within the **Always Free** limits.

## Before You Start

* You need a valid international bank card for verification when signing up (usually Visa/Mastercard). Oracle does not charge it for Always Free resources; it is only used for identity/payment verification.
* Registration may take anywhere from a few minutes to several hours for review, depending on your country.
* Sometimes, when creating an ARM machine (recommended), you may see **"Out of host capacity"**. This is common with Oracle Free Tier. The solution is to try again (or change the Availability Domain) until it works.

## 1. Create the Account and VM

1. Sign up at [Oracle Cloud Sign Up](https://signup.oraclecloud.com?utm_source=chatgpt.com) (choose an Always Free eligible region close to you if possible).

2. From the menu, go to **Compute → Instances → Create Instance**.

3. **Image:** Choose **Ubuntu 22.04** (or 24.04).

4. **Shape:** Click **Change Shape → Ampere (ARM) → VM.Standard.A1.Flex** → choose, for example, **2 OCPUs / 12 GB RAM** (within the Always Free quota, which can provide up to 4 OCPUs / 24 GB for free).

   If you get an **"Out of capacity"** error, try again or use **VM.Standard.E2.1.Micro** (AMD). It is weaker but is often immediately available and is sufficient for this bot as well.

5. **Add SSH keys:** Choose **Generate a key pair for me** and download the private key (`.key` or `.pem`). You will need it to connect to the server. Alternatively, provide your own public key if you already have one.

6. Leave the remaining settings at their defaults and click **Create**. Wait until the instance status becomes **Running**, then copy the **Public IP Address**.

## 2. Connect via SSH

From your computer (Windows: use PowerShell or PuTTY):

```bash
ssh -i path\to\your-key.key ubuntu@YOUR_PUBLIC_IP
```

## 3. Prepare the Server

```bash
sudo apt update && sudo apt upgrade -y

sudo apt install -y python3 python3-venv python3-pip git build-essential
```

## 4. Clone the Project

```bash
git clone https://github.com/MiilouDz/aliexoo.git

cd aliexoo
```

## 5. Virtual Environment + Dependencies

```bash
python3 -m venv .venv

source .venv/bin/activate

pip install --upgrade pip

pip install -r requirements.txt
```

## 6. `.env` File

```bash
cp .env.example .env

nano .env
```

Fill in the usual values (`TELEGRAM_BOT_TOKEN`, AliExpress keys, `TELEGRAM_API_ID`/`HASH`, `SOURCE_CHANNELS_DZ`/`FR`, `TARGET_CHANNEL`).

Leave `TELETHON_SESSION`/`STATE_FILE` at their default values — the files will remain directly inside the project directory on this server, so there is no need for a separate disk.

Save with **Ctrl+O**, press **Enter**, then exit with **Ctrl+X**.

## 7. The `monitor.session` File

Since SSH here is a real interactive connection, you have two options:

### Upload Your Existing File

From your own computer, **not from inside the server**:

```bash
scp -i path\to\your-key.key monitor.session ubuntu@YOUR_PUBLIC_IP:~/aliexoo/monitor.session
```

### Or Log In Again Directly From the Server

```bash
python telethon_login.py
```

## 8. Test It Manually First

```bash
python aliexpress_service.py
```

Make sure you see:

```text
Logged in. Catching up on source channels...
Monitoring channels: [...]
```

Then stop it with **Ctrl+C** — this is only a test.

## 9. Run It Permanently with systemd

This will make the bot:

* Start automatically when the server reboots.
* Restart automatically if the bot crashes.

Copy the attached `aliexoo.service` file to the server using the same method as uploading `monitor.session` in Step 7, or create it directly:

```bash
sudo nano /etc/systemd/system/aliexoo.service
```

Paste the contents of `aliexoo.service` (adjust `ubuntu` and the paths if your username or project directory is different), then save and exit.

Run:

```bash
sudo systemctl daemon-reload

sudo systemctl enable aliexoo

sudo systemctl start aliexoo
```

## 10. Verify That It Is Running

```bash
sudo systemctl status aliexoo

journalctl -u aliexoo -f
```

`journalctl -f` displays the logs in real time. Exit with **Ctrl+C** — the service will continue running; this only stops viewing the logs.

## Updating the Code Later

```bash
cd ~/aliexoo

git pull

source .venv/bin/activate

pip install -r requirements.txt   # Only if the dependencies changed

sudo systemctl restart aliexoo
```

## Troubleshooting

* **`journalctl -u aliexoo` shows nothing or the service keeps restarting:** Run it manually with `python aliexpress_service.py` to see the complete error instead of the shorter systemd output.

* **Strange connection problems:** Check:

```bash
sudo iptables -L
```

or:

```bash
sudo ufw status
```

Oracle's default images may contain basic firewall rules, but they generally do not restrict the **outbound** connections required by this project.

* Other notes (albums, videos, etc.) from `README.md` apply here without changes.
