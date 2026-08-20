# fnack Production Deployment & Migration Guide

This document explains how to export, transfer, and run **fnack** on another machine.

---

## 1. Export the Docker Image (Current Machine)

You can save the production Docker image to a compressed `.tar.gz` archive:

```bash
docker save fnack:latest | gzip > fnack_image.tar.gz
```

*(Optional)* If you want to backup your existing database and configurations as well:
```bash
docker run --rm -v fnack_config:/config -v $(pwd):/backup alpine tar czf /backup/fnack_config_backup.tar.gz -C /config .
```

---

## 2. Transfer to Another Machine

Transfer `fnack_image.tar.gz` (and `docker-compose.yml`) to the target machine via `scp`, `rsync`, or USB drive:

```bash
# Example using scp:
scp fnack_image.tar.gz docker-compose.yml user@remote-ip:~/fnack/
```

---

## 3. Load & Run on the New Machine

On the target machine:

### Step 3A: Load the Docker Image
```bash
gunzip -c fnack_image.tar.gz | docker load
```
Verify the image is loaded:
```bash
docker images | grep fnack
```

### Step 3B: (Optional) Restore Existing Configuration/Database
If you backed up your config volume in Step 1:
```bash
docker volume create fnack_config
docker run --rm -v fnack_config:/config -v $(pwd):/backup alpine tar xzf /backup/fnack_config_backup.tar.gz -C /config
```

### Step 3C: Start the Container

#### Option 1: Using Docker Compose (Recommended)
Make sure `docker-compose.yml` is in the folder, then run:
```bash
docker compose up -d
```

#### Option 2: Using Standalone Docker Run
```bash
docker run -d \
  --name fnack \
  --restart unless-stopped \
  -p 4688:4688 \
  -v fnack_config:/config \
  -v fnack_downloads:/downloads \
  -v fnack_music:/music \
  -e MAX_CONCURRENT_DOWNLOADS=3 \
  fnack:latest
```

*If you prefer bind mounts to host folders instead of named volumes:*
```bash
docker run -d \
  --name fnack \
  --restart unless-stopped \
  -p 4688:4688 \
  -v /path/to/my/config:/config \
  -v /path/to/my/downloads:/downloads \
  -v /path/to/my/music:/music \
  -e MAX_CONCURRENT_DOWNLOADS=3 \
  fnack:latest
```

---

## 4. Verification

Open your web browser and navigate to:
```text
http://<server-ip>:4688
```

Check container health status and logs:
```bash
docker ps
docker logs -f fnack
```
