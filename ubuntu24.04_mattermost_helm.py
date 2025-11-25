"""
This script serves to set up mattermost with kubernetes and helm based on a guide by Claude AI

https://claude.ai/public/artifacts/231822ef-fd8b-49e5-9c7c-ef4b8f848535?fullscreen=true
"""

import time
import os
import sys
import os
from os.path import join as pjoin
from textwrap import dedent

from packaging import version
from ipydex import IPS, activate_ips_on_exception

min_du_version = version.parse("0.12.0")
try:
    import deploymentutils as du

    vsn = version.parse(du.__version__)
    if vsn < min_du_version:
        print(f"You need to install `deploymentutils` in version {min_du_version} or later. Quit.")
        exit()


except ImportError as err:
    print("You need to install the package `deploymentutils` to run this script.")


# call this before running the script:
# eval $(ssh-agent); ssh-add -t 10m


# simplify debugging
activate_ips_on_exception()


# -------------------------- Essential Config section  ------------------------

config = du.get_nearest_config("config.toml")

remote = config("remote")
user = config("user")

# -------------------------- Begin Optional Config section -------------------------
# if you know what you are doing you can adapt these settings to your needs

PHP_VERSION = "8.3"

# this is the root dir of the project (where setup.py lies)
# if you maintain more than one instance (and deploy.py lives outside the project dir, this has to change)
project_src_path = os.path.dirname(du.get_dir_of_this_file())

temp_workdir = pjoin(du.get_dir_of_this_file(), "tmp_workdir")  # this will be deleted/overwritten

# ensure clean workdir
os.system(f"rm -rf {temp_workdir}")
os.makedirs(temp_workdir)

c = du.StateConnection(remote, user=user, target="remote", parse_args=True)

c.run(f"echo hello new vm with os:")
# get name of Linux distribution
res = c.run(f"lsb_release -a")



# ssh key handling:

# remove from known_hosts:
# f"ssh-keygen -R {config("remote")}"

# add keys
# f"ssh-keyscan {config("remote")} >> ~/.ssh/known_hosts"
# f"ssh-keyscan -t ed25519 {config("remote")} >> ~/.ssh/known_hosts"




def install_starship_tmux_mc(c: du.StateConnection):
    """
    Install some tools which are not strictly necessary, but significantly simplify interactive debugging
    """
    c.run(f"mkdir -p ~/tmp")
    c.run(f"mkdir -p ~/bin")
    c.chdir("~/tmp")

    if not c.check_existence("install_starship.sh"):
        c.run(f"curl  https://starship.rs/install.sh > install_starship.sh")
    c.run(f"sh install_starship.sh --bin-dir ~/bin --yes")


    bashrc_content = \
    r"""
    # make bash autocomplete with up/down arrow if in interactive mode
    if [ -t 1 ]
    then
        bind '"\e[A":history-search-backward'
        bind '"\e[B":history-search-forward'
    fi

    export EDITOR=mcedit
    export VISUAL=mcedit

    eval "$(~/bin/starship init bash)"
    """

    c.chdir("~/")

    c.string_to_file(bashrc_content, "~/.bashrc", mode=">>")

    c.run(f"sudo apt update && sudo apt upgrade -y")
    c.run(f"apt install --assume-yes tmux rsync")

    # midnight commander with lynx like motion
    c.run(f"apt install --assume-yes mc")
    c.run(f"mkdir -p ~/.config/mc")
    # trailing slash at source is important
    c.rsync_upload("config_files/mc/", "~/.config/mc", "remote")

def install_mattermost_with_helm(c: du.StateConnection):

    # ensure that we have left possible subdirectories
    c.dir = None
    c.run("sudo apt update && sudo apt upgrade -y")
    c.run("sudo apt install -y curl wget git apt-transport-https ca-certificates ufw")

    # firewall
    c.run("sudo ufw allow 22/tcp")  # ssh
    c.run("sudo ufw allow 80/tcp")  # http
    c.run("sudo ufw allow 443/tcp")  # https
    c.run("sudo ufw allow 6443/tcp")  # kubernetes api

    # without --force this asks for confirmation
    c.run("sudo ufw --force enable")

    # 1.3 Disable Swap (Required for Kubernetes)

    c.run("sudo swapoff -a")
    c.run("sudo sed -i '/ swap / s/^/#/' /etc/fstab")

    c.run("""curl -sfL https://get.k3s.io | sh -s - \\
        --write-kubeconfig-mode 644 \\
        --disable traefik""")

    # verify installation
    c.run("sudo systemctl status k3s")
    c.run("kubectl get nodes")

    # Set Up kubectl for Your User
    c.run("mkdir -p ~/.kube")
    c.run("sudo cp /etc/rancher/k3s/k3s.yaml ~/.kube/config")
    c.run("sudo chown $(id -u):$(id -g) ~/.kube/config")
    c.run("export KUBECONFIG=~/.kube/config")
    c.set_env("KUBECONFIG", "~/.kube/config")
    c.run("echo 'export KUBECONFIG=~/.kube/config' >> ~/.bashrc")

    # install helm
    c.run("curl https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash")
    c.run("helm version")

    # Install NGINX Ingress Controller
    c.run("helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx")
    c.run("helm repo update")

    # Verify kubectl configuration before running helm (NEW!!)
    c.run("echo 'Current KUBECONFIG:' && echo $KUBECONFIG")
    c.run("kubectl config current-context")
    c.run("kubectl cluster-info")

    # Check if ingress-nginx is already installed
    helm_check = c.run("helm list -n ingress-nginx", warn=True, hide=True)
    if "ingress-nginx" in helm_check.stdout:
        print("ingress-nginx already installed, skipping installation")
    else:
        c.run(
            "helm install ingress-nginx ingress-nginx/ingress-nginx "
            "--namespace ingress-nginx "
            "--create-namespace "
            "--set controller.service.type=LoadBalancer"
        )

    c.run("kubectl get pods -n ingress-nginx")
    c.run("kubectl get svc -n ingress-nginx")

    # Install cert-manager (For HTTPS)
    c.run("helm repo add jetstack https://charts.jetstack.io")
    c.run("helm repo update")

    # Check if cert-manager is already installed
    cert_manager_check = c.run("helm list -n cert-manager", warn=True, hide=True)
    if "cert-manager" in cert_manager_check.stdout:
        print("cert-manager already installed, skipping installation")
    else:
        c.run(
            "helm install cert-manager jetstack/cert-manager "
            "--namespace cert-manager "
            "--create-namespace "
            "--set crds.enabled=true"
        )

    cluster_issuer = dedent(f"""
    apiVersion: cert-manager.io/v1
    kind: ClusterIssuer
    metadata:
      name: letsencrypt-prod
    spec:
      acme:
        server: https://acme-v02.api.letsencrypt.org/directory
        email: {config('mattermost::letsencrypt_email')}
        privateKeySecretRef:
          name: letsencrypt-prod
        solvers:
        - http01:
            ingress:
              class: nginx
    """)
    c.string_to_file(cluster_issuer, "~/cluster-issuer.yaml", mode=">")
    c.run("kubectl apply -f cluster-issuer.yaml")

    # Part 6: Create Mattermost Namespace & Storage
    # Check if namespace exists
    ns_check = c.run("kubectl get namespace mattermost", warn=True, hide=True)
    if ns_check.return_code != 0:
        c.run("kubectl create namespace mattermost")
    else:
        print("mattermost namespace already exists, skipping creation")
    mattermost_storage = dedent(f"""
    ---
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: mattermost-data
      namespace: mattermost
    spec:
      accessModes:
        - ReadWriteOnce
      storageClassName: local-path
      resources:
        requests:
          storage: 10Gi
    ---
    apiVersion: v1
    kind: PersistentVolumeClaim
    metadata:
      name: postgres-data
      namespace: mattermost
    spec:
      accessModes:
        - ReadWriteOnce
      storageClassName: local-path
      resources:
        requests:
          storage: 10Gi
    """)
    c.string_to_file(mattermost_storage, "~/mattermost-storage.yaml", mode=">")
    c.run("kubectl apply -f mattermost-storage.yaml")

    # Part 7: Deploy PostgreSQL

    postgres_config = dedent(f"""
    ---
    apiVersion: v1
    kind: Secret
    metadata:
      name: postgres-secret
      namespace: mattermost
    type: Opaque
    stringData:
      POSTGRES_USER: {config('mattermost::psql_user')}
      POSTGRES_PASSWORD: "{config('mattermost::psql_password')}"  # Change this!
      POSTGRES_DB: mattermost
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: postgres
      namespace: mattermost
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: postgres
      template:
        metadata:
          labels:
            app: postgres
        spec:
          containers:
          - name: postgres
            image: postgres:15-alpine
            ports:
            - containerPort: 5432
            envFrom:
            - secretRef:
                name: postgres-secret
            volumeMounts:
            - name: postgres-storage
              mountPath: /var/lib/postgresql/data
              subPath: postgres
            resources:
              requests:
                memory: "512Mi"
                cpu: "250m"
              limits:
                memory: "1Gi"
                cpu: "500m"
          volumes:
          - name: postgres-storage
            persistentVolumeClaim:
              claimName: postgres-data
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: postgres
      namespace: mattermost
    spec:
      selector:
        app: postgres
      ports:
      - port: 5432
        targetPort: 5432

    """)
    c.string_to_file(postgres_config, "~/postgres.yaml", mode=">")
    c.run("kubectl apply -f postgres.yaml")
    c.run("kubectl get pods -n mattermost")

    # Part 8: Deploy Mattermost
    MM_SQLSETTINGS_DATASOURCE = f"postgres://{config('mattermost::psql_user')}:{config('mattermost::psql_password')}@postgres:5432/mattermost?sslmode=disable&connect_timeout=10"
    mattermost_config = dedent(f"""
    ---
    apiVersion: v1
    kind: Secret
    metadata:
      name: mattermost-secret
      namespace: mattermost
    type: Opaque
    stringData:
      MM_SQLSETTINGS_DATASOURCE: "{MM_SQLSETTINGS_DATASOURCE}"
    ---
    apiVersion: apps/v1
    kind: Deployment
    metadata:
      name: mattermost
      namespace: mattermost
    spec:
      replicas: 1
      selector:
        matchLabels:
          app: mattermost
      template:
        metadata:
          labels:
            app: mattermost
        spec:
          containers:
          - name: mattermost
            image: mattermost/mattermost-team-edition:latest
            ports:
            - containerPort: 8065
            env:
            - name: MM_SQLSETTINGS_DRIVERNAME
              value: "postgres"
            - name: MM_SERVICESETTINGS_SITEURL
              value: "{config('mattermost::site_url')}"
            - name: MM_SERVICESETTINGS_LISTENADDRESS
              value: ":8065"
            - name: MM_FILESETTINGS_DIRECTORY
              value: "/mattermost/data"
            envFrom:
            - secretRef:
                name: mattermost-secret
            volumeMounts:
            - name: mattermost-data
              mountPath: /mattermost/data
            resources:
              requests:
                memory: "1Gi"
                cpu: "500m"
              limits:
                memory: "2Gi"
                cpu: "1000m"
            livenessProbe:
              httpGet:
                path: /api/v4/system/ping
                port: 8065
              initialDelaySeconds: 60
              periodSeconds: 10
            readinessProbe:
              httpGet:
                path: /api/v4/system/ping
                port: 8065
              initialDelaySeconds: 30
              periodSeconds: 5
          volumes:
          - name: mattermost-data
            persistentVolumeClaim:
              claimName: mattermost-data
    ---
    apiVersion: v1
    kind: Service
    metadata:
      name: mattermost
      namespace: mattermost
    spec:
      selector:
        app: mattermost
      ports:
      - port: 8065
        targetPort: 8065
    """)
    c.string_to_file(mattermost_config, "~/mattermost.yaml", mode=">")
    c.run("kubectl apply -f mattermost.yaml")

    # Configure Ingress with TLS

    mattermost_ingress_config = dedent(f"""
    apiVersion: networking.k8s.io/v1
    kind: Ingress
    metadata:
      name: mattermost-ingress
      namespace: mattermost
      annotations:
        cert-manager.io/cluster-issuer: "letsencrypt-prod"
        nginx.ingress.kubernetes.io/proxy-body-size: "50m"
        nginx.ingress.kubernetes.io/proxy-read-timeout: "600"
        nginx.ingress.kubernetes.io/proxy-send-timeout: "600"
        nginx.ingress.kubernetes.io/proxy-buffering: "off"
    spec:
      ingressClassName: nginx
      tls:
      - hosts:
        - "{config('mattermost::site_url').replace('https://', '').replace('http://', '')}"
        secretName: mattermost-tls
      rules:
      - host: "{config('mattermost::site_url').replace('https://', '').replace('http://', '')}"
        http:
          paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: mattermost
                port:
                  number: 8065
    """)
    c.string_to_file(mattermost_ingress_config, "~/mattermost-ingress.yaml", mode=">")

    # Check if certificate already exists to avoid unnecessary recreation
    cert_check = c.run("kubectl get certificate mattermost-tls -n mattermost", warn=True, hide=True)
    if cert_check.return_code == 0:
        print("Certificate already exists, skipping ingress recreation to avoid Let's Encrypt rate limits")
    else:
        c.run("kubectl apply -f mattermost-ingress.yaml")

    # Part 10: Verify Deployment

    c.run("kubectl get all -n mattermost")
    c.run("kubectl get ingress -n mattermost")
    c.run("kubectl get certificate -n mattermost")

    # 10.2 Check Logs if Needed

    c.run("kubectl logs -n mattermost deployment/mattermost")

    c.run("kubectl logs -n mattermost deployment/postgres")

    # 10.3 Wait for certificate to be ready and backup Let's Encrypt files
    print("Waiting for certificate to be ready...")

    # Wait up to 10 minutes for certificate to be ready
    for i in range(60):  # 60 attempts, 10 seconds each = 10 minutes max
        cert_status = c.run("kubectl get certificate mattermost-tls -n mattermost -o jsonpath='{.status.conditions[?(@.type==\"Ready\")].status}'", warn=True, hide=True)
        if cert_status.stdout.strip() == "True":
            print("Certificate is ready!")
            break
        print(f"Certificate not ready yet, waiting... (attempt {i+1}/60)")
        time.sleep(10)
    else:
        print("Warning: Certificate may not be ready yet, but proceeding with backup attempt")

    # Create local backup directory
    backup_dir = "./lets_encrypt_backup"
    os.makedirs(backup_dir, exist_ok=True)

    # Download Let's Encrypt certificate files
    print("Backing up Let's Encrypt certificates...")

    # Get the secret name and download the TLS secret
    c.run("kubectl get secret mattermost-tls -n mattermost -o yaml > ~/mattermost-tls-secret.yaml")
    c.rsync_download("~/mattermost-tls-secret.yaml", f"{backup_dir}/mattermost-tls-secret.yaml", "remote")

    # Download the Let's Encrypt account key and other cert-manager secrets
    c.run("kubectl get secret letsencrypt-prod -n cert-manager -o yaml > ~/letsencrypt-prod-secret.yaml", warn=True)
    c.rsync_download("~/letsencrypt-prod-secret.yaml", f"{backup_dir}/letsencrypt-prod-secret.yaml", "remote", warn=True)

    # Download cluster issuer configuration
    c.run("kubectl get clusterissuer letsencrypt-prod -o yaml > ~/letsencrypt-prod-clusterissuer.yaml")
    c.rsync_download("~/letsencrypt-prod-clusterissuer.yaml", f"{backup_dir}/letsencrypt-prod-clusterissuer.yaml", "remote")

    # Create a restore script for future use
    restore_script = dedent(f"""#!/bin/bash
    # Script to restore Let's Encrypt certificates
    # Run this before applying the ingress configuration

    echo "Restoring Let's Encrypt certificates..."

    # Apply the cluster issuer first (tells cert-manager how to communicate with Let's Encrypt)
    # Contains the ACME server URL, your email, and challenge solver configuration
    # Does NOT trigger new certificate requests - it just sets up the issuer for future use

    kubectl apply -f letsencrypt-prod-clusterissuer.yaml

    # Wait a moment for cert-manager to be ready
    sleep 5

    # Apply the account secret
    # (Restores the Let's Encrypt account private key)
    # Critical for avoiding rate limits - without this, cert-manager would create a
    # new account and potentially hit duplicate certificate limits

    kubectl apply -f letsencrypt-prod-secret.yaml

    # Apply the TLS secret
    # Restores the actual TLS certificate and private key
    # (the mattermost-tls secret in mattermost namespace)
    # This contains the SSL certificate that nginx uses to serve HTTPS traffic
    # Immediately enables HTTPS without waiting for certificate generation

    kubectl apply -f mattermost-tls-secret.yaml

    echo "Certificates restored. You can now apply your ingress configuration."
    """)

    with open(f"{backup_dir}/restore_certificates.sh", "w") as f:
        f.write(restore_script)

    print(f"Let's Encrypt certificates backed up to {backup_dir}/")
    print(f"To restore certificates on a fresh installation, run: bash {backup_dir}/restore_certificates.sh")

    # 10.4 Access Mattermost
    # Navigate to https://chat.yourdomain.com and create your admin account.

    time.sleep(10)
    print(f'Now you should be able to access the Mattermost UI at {config("mattermost::site_url")}')
    # IPS()


install_starship_tmux_mc(c)
install_mattermost_with_helm(c)
exit()
