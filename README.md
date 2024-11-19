# Azure Snapshot Manager

## Overview
This application is designed to manage Azure VM snapshots effectively. It enables creating self-destructing snapshots with a defined Time-To-Live (TTL) and provides features to exclude specific VMs based on keywords.

The app is built using Flask and asyncio for asynchronous operations and leverages the Azure CLI to interact with Azure resources.

---

## Requirements

### Software
- Python 3.7 or higher
- Azure CLI
- Flask
- asyncio
- Required Python packages (see below)

### Python Dependencies
Install the required Python packages using:
```bash
pip install flask asyncio
```

