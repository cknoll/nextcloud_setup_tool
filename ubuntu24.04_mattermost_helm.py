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


def prepare01(c: du.StateConnection):
    c.run("sudo apt update && sudo apt upgrade -y")
    c.run("sudo apt install -y curl wget git apt-transport-https ca-certificates")

    # firewall
    c.run("sudo ufw allow 22/tcp")  # ssh
    c.run("sudo ufw allow 80/tcp")  # http
    c.run("sudo ufw allow 443/tcp")  # https
    c.run("sudo ufw allow 6443/tcp")  # kubernetes api

    # TODO: this asks for confirmation
    c.run("sudo ufw enable")

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

    # if the following was already run use `helm -n ingress-nginx delete ingress-nginx` to undo it
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
        email: your-email@example.com  # Change this!
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
    c.run("kubectl create namespace mattermost")
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
    c.run("echo 'THE FOLLOWING MIGHT TAKE SOME MINUTES' && kubectl get pods -n mattermost -w")

    # Part 8: Deploy Mattermost
    MM_SQLSETTINGS_DATASOURCE = "postgres://mattermost:YourSecurePassword123!@postgres:5432/mattermost?sslmode=disable&connect_timeout=10"
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
        - {config('mattermost::site_url')}  # Change this!
        secretName: mattermost-tls
      rules:
      - host: {config('mattermost::site_url')}  # Change this!
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
    c.run("kubectl apply -f mattermost-ingress.yaml")

    # Part 10: Verify Deployment

    c.run("kubectl get all -n mattermost")
    c.run("kubectl get ingress -n mattermost")
    c.run("kubectl get certificate -n mattermost")

    # 10.2 Check Logs if Needed

    c.run("kubectl logs -n mattermost deployment/mattermost")
    c.run("kubectl logs -n mattermost deployment/postgres")

    # 10.3 Access Mattermost
    # Navigate to https://chat.yourdomain.com and create your admin account.

    # c.run("")
    # c.run("")
    IPS()

prepare01(c)
exit()
