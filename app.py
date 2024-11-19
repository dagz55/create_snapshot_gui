from flask import Flask, send_from_directory, request, jsonify
import asyncio
import datetime
import json
import os
from collections import defaultdict
import subprocess

app = Flask(__name__, static_folder='azure-snapshot-manager/build', static_url_path='')

# Global variables
log_dir = "logs"
snap_rid_list_file = "snap_rid_list.txt"
inventory_file = 'linux_vm-inventory.csv'
TTL_DURATION = 7  # Number of days for the snapshots to be valid


def get_vm_info(hostname, inventory_file):
    with open(inventory_file, 'r') as f:
        for line in f:
            if hostname in line:
                return line.strip()
    return None

def extract_vm_info(host_file, exclude_keywords=None):
    if not os.path.exists(inventory_file):
        return None, f"Error: Inventory file '{inventory_file}' not found."

    if not os.path.exists(host_file):
        return None, f"Error: List file '{host_file}' not found."

    vm_list = []
    excluded_vms = []
    with open(host_file, 'r') as f:
        hostnames = f.read().splitlines()
        for hostname in hostnames:
            vm_info = get_vm_info(hostname, inventory_file)
            if vm_info:
                # Check if any exclude keyword matches
                if exclude_keywords and any(keyword.lower() in vm_info.lower() for keyword in exclude_keywords):
                    excluded_vms.append(hostname)
                    continue
                vm_list.append(vm_info)
            else:
                print(f"Warning: Information not found for hostname '{hostname}'")

    if excluded_vms:
        print(f"Excluded VMs based on keywords: {', '.join(excluded_vms)}")
        
    return vm_list, None

async def run_az_command(command, max_retries=3, delay=5):
    for attempt in range(max_retries):
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            return stdout.decode().strip(), stderr.decode().strip(), process.returncode
        else:
            print(f"Command failed (attempt {attempt + 1}): {command}")
            print(f"Error: {stderr.decode().strip()}")
            if attempt < max_retries - 1:
                print(f"Retrying in {delay} seconds...")
                await asyncio.sleep(delay)
    return "", stderr.decode().strip(), process.returncode

def write_log(log_file, message, is_deletion=False):
    timestamp = datetime.datetime.now()
    log_entry = f"{timestamp}: {'[DELETION] ' if is_deletion else ''}{message}\n"
    with open(log_file, "a") as f:
        f.write(log_entry)

def write_snapshot_rid(snapshot_id, snapshot_name, expiration_date, user_id):
    with open(snap_rid_list_file, "a") as f:
        f.write(f"{snapshot_id},{snapshot_name},{expiration_date},{user_id}\n")

async def get_current_user_id():
    stdout, stderr, returncode = await run_az_command(
        "az account show --query user.name -o tsv"
    )
    if returncode != 0:
        print(f"Failed to get current user ID. Error: {stderr}")
        return None
    else:
        return stdout.strip()

async def process_vm(resource_id, vm_name, resource_group, disk_id, chg_number, timestamp, log_file, user_id):
    write_log(log_file, f"Processing VM: {vm_name}")
    write_log(log_file, f"Resource ID: {resource_id}")
    write_log(log_file, f"Resource group: {resource_group}")
    write_log(log_file, f"User ID: {user_id}")  # Log the user ID

    snapshot_name = f"RH_{chg_number}_{vm_name}_{timestamp}"
    expiration_date = (datetime.datetime.now() + datetime.timedelta(days=TTL_DURATION)).strftime('%Y-%m-%d')

    # Include the user ID and drtier as tags in the snapshot
    stdout, stderr, returncode = await run_az_command(
        f"az snapshot create --name {snapshot_name} --resource-group {resource_group} --source {disk_id} --tags CreatedByUserId={user_id} drtier=NR"
    )

    if returncode != 0:
        write_log(log_file, f"Failed to create snapshot for VM: {vm_name}")
        write_log(log_file, f"Error: {stderr}")
        return vm_name, "Failed to create snapshot"
    else:
        write_log(log_file, f"Snapshot created: {snapshot_name}")
        write_log(log_file, json.dumps(json.loads(stdout), indent=2))

        snapshot_data = json.loads(stdout)
        snapshot_id = snapshot_data.get('id')
        if snapshot_id:
            write_snapshot_rid(snapshot_id, snapshot_name, expiration_date, user_id)
            write_log(log_file, f"Snapshot resource ID added to snap_rid_list.txt: {snapshot_id}")
            return vm_name, snapshot_name
        else:
            write_log(log_file, f"Warning: Could not extract snapshot resource ID for {snapshot_name}")
            return vm_name, "Failed to extract snapshot ID"

def group_vms_by_subscription(vm_list):
    grouped_vms = defaultdict(list)
    for line in vm_list:
        resource_id, vm_name = line.split()
        subscription_id = resource_id.split("/")[2]
        grouped_vms[subscription_id].append((resource_id, vm_name))
    return grouped_vms

async def main(host_file, chg_number, ttl_duration, exclude_keywords=None):
    global TTL_DURATION
    TTL_DURATION = ttl_duration

    # Create log directory
    os.makedirs(log_dir, exist_ok=True)

    # Generate log file names
    timestamp = datetime.datetime.now().strftime('%Y%m%d%H%M%S')
    log_file = os.path.join(log_dir, f"snapshot_creation_log_{timestamp}.txt")
    summary_file = os.path.join(log_dir, f"snapshot_summary_{timestamp}.txt")

    write_log(log_file, f"CHG Number: {chg_number}")
    write_log(log_file, f"Snapshot Time-to-Live: {TTL_DURATION} days")

    # Get the current user ID from Azure CLI
    user_id = await get_current_user_id()
    if not user_id:
        return {"error": "Failed to retrieve user ID from Azure CLI. Ensure you are logged in with 'az login'."}
    write_log(log_file, f"User ID: {user_id}")  # Log the user ID

    vm_list, error = extract_vm_info(host_file, exclude_keywords)
    if error:
        return {"error": error}

    if not vm_list:
        return {"error": "No valid VM information found."}

    total_vms = len(vm_list)
    grouped_vms = group_vms_by_subscription(vm_list)

    successful_snapshots = []
    failed_snapshots = []

    for subscription_id, vms in grouped_vms.items():
        # Switch to the current subscription
        stdout, stderr, returncode = await run_az_command(f"az account set --subscription {subscription_id}")
        if returncode != 0:
            write_log(log_file, f"Failed to set subscription ID: {subscription_id}")
            write_log(log_file, f"Error: {stderr}")
            for _, vm_name in vms:
                failed_snapshots.append((vm_name, "Failed to set subscription"))
            continue

        write_log(log_file, f"Switched to subscription: {subscription_id}")

        tasks = []
        for resource_id, vm_name in vms:
            # Get resource group and disk ID for each VM
            stdout, stderr, returncode = await run_az_command(
                f"az vm show --ids {resource_id} --query '{{resourceGroup:resourceGroup, diskId:storageProfile.osDisk.managedDisk.id}}' -o json"
            )
            if returncode != 0:
                write_log(log_file, f"Failed to get VM details for {vm_name}")
                write_log(log_file, f"Error: {stderr}")
                failed_snapshots.append((vm_name, "Failed to get VM details"))
                continue

            vm_details = json.loads(stdout)
            resource_group = vm_details['resourceGroup']
            disk_id = vm_details['diskId']

            task = asyncio.create_task(process_vm(resource_id, vm_name, resource_group, disk_id, chg_number, timestamp, log_file, user_id))
            tasks.append(task)

        results = await asyncio.gather(*tasks)
        for vm_name, result in results:
            if "Failed" not in result:
                successful_snapshots.append((vm_name, result))
            else:
                failed_snapshots.append((vm_name, result))

    # Write summary to file
    with open(summary_file, "w") as f:
        f.write("Self-Destruct Snapshot Creation Summary\n")
        f.write("=======================================\n\n")
        f.write(f"Total VMs processed: {total_vms}\n")
        f.write(f"Successful snapshots: {len(successful_snapshots)}\n")
        f.write(f"Failed snapshots: {len(failed_snapshots)}\n")
        f.write(f"Time-to-Live: {TTL_DURATION} days\n\n")
        f.write("Successful snapshots:\n")
        for vm, snapshot in successful_snapshots:
            f.write(f"- {vm}: {snapshot}\n")
        f.write("\nFailed snapshots:\n")
        for vm, error in failed_snapshots:
            f.write(f"- {vm}: {error}\n")

    return {
        'total_vms': total_vms,
        'successful_snapshots': len(successful_snapshots),
        'failed_snapshots': len(failed_snapshots),
        'log_file': log_file,
        'summary_file': summary_file,
        'snap_rid_list_file': snap_rid_list_file
    }

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    if path != "" and os.path.exists(app.static_folder + '/' + path):
        return send_from_directory(app.static_folder, path)
    else:
        return send_from_directory(app.static_folder, 'index.html')

@app.route('/api/azure-login', methods=['GET'])
def azure_login():
    try:
        # Run the 'az login' command
        process = subprocess.Popen(['az', 'login'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        
        if process.returncode == 0:
            # Parse the JSON output to get the login URL
            login_info = json.loads(stdout)
            login_url = login_info[0]['verificationUrl']
            user_code = login_info[0]['userCode']
            
            return jsonify({
                'login_url': login_url,
                'user_code': user_code
            })
        else:
            return jsonify({'error': 'Failed to initiate Azure login'}), 500
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/create-snapshots', methods=['POST'])
def create_snapshots():
    host_file = request.json.get('host_file', 'snapshot_vmlist.txt')
    chg_number = request.json.get('chg_number')
    ttl_duration = int(request.json.get('ttl_duration', TTL_DURATION))
    exclude_keywords = request.json.get('exclude_keywords', [])

    # Call the main function with the provided parameters
    result = asyncio.run(main(host_file, chg_number, ttl_duration, exclude_keywords))

    return jsonify(result)

if __name__ == '__main__':
    app.run(debug=True)
