"""
This script serves to create a golden image for a nextcloud installation.
It is based on the instructions of https://www.youtube.com/watch?v=r--pQtwQMv0
("Make Nextcloud fast! Full tutorial and server setup!")

However, that video is for Ubuntu 22.04 and this script is for Ubuntu 24.04.
It also incorporates some customization (e.g. starship and mc to simplify debugging)
"""

import time
import os
import sys
import os
from os.path import join as pjoin
from textwrap import dedent

from packaging import version
from ipydex import IPS, activate_ips_on_exception

min_du_version = version.parse("0.9.0")
try:
    # this is not listed in the requirements because it is not needed on the deployment server
    # noinspection PyPackageRequirements,PyUnresolvedReferences
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

c = du.StateConnection(remote, user=user, target="remote")

c.run(f"echo hello new vm with os:")
# get name of Linux distribution
res = c.run(f"lsb_release -a")


def install_starship_tmux_mc(c: du.StateConnection):
    c.run(f"mkdir -p ~/tmp")
    c.run(f"mkdir -p ~/bin")
    c.chdir("~/tmp")
    #c.run(f"curl  https://starship.rs/install.sh > install_starship.sh")
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


    c.string_to_file(bashrc_content, "~/.bashrc", mode=">>")

    c.run(f"sudo apt update && sudo apt upgrade -y")
    c.run(f"apt install --assume-yes tmux rsync")

    # midnight commander with lynx like motion
    c.run(f"apt install --assume-yes mc")
    c.run(f"mkdir -p ~/.config/mc")
    # trailing slash at source is important
    c.rsync_upload("config_files/mc/", "~/.config/mc", "remote")


def nc_prep01(c: du.StateConnection):
    c.run("apt install --assume-yes curl wget gnupg2 lsb-release ca-certificates")
    c.run("apt install --assume-yes apache2")
    c.run("apt install --assume-yes imagemagick memcached libmemcached-tools mariadb-server unzip smbclient")
    php_modules = "{cli,common,curl,gd,mbstring,xml,zip,intl,gmp,bcmath,mysql,imagick,memcached,apcu}"
    c.run(f"apt install --assume-yes php{PHP_VERSION}-fpm php{PHP_VERSION}-{php_modules}")


def nc_prep02(c: du.StateConnection):
    c.run(f"a2enconf php{PHP_VERSION}-fpm")

    content = dedent(f"""
    <VirtualHost *:80>
            Protocols h2 h2c http/1.1
            ServerName {config("server_name")}
            DocumentRoot /var/www/nextcloud

            <IfModule mod_headers.c>
            Header always set Strict-Transport-Security "max-age=15552000; includeSubDomains"
            </IfModule>

            <FilesMatch \.php$>
            SetHandler "proxy:unix:/var/run/php/php{PHP_VERSION}-fpm.sock|fcgi://localhost"
            </FilesMatch>

            <Directory /var/www/nextcloud/>
                    Satisfy Any
                    Require all granted
                    Options FollowSymlinks MultiViews
                    AllowOverride All
                    <IfModule mod_dav.c>
                            Dav off
                    </IfModule>
            </Directory>

            ErrorLog /var/log/apache2/nextcloud-error.log
            CustomLog /var/log/apache2/nextcloud-access.log common
    </VirtualHost>
    """)

    c.string_to_file(content, "/etc/apache2/sites-available/nextcloud.conf", mode=">")

    # enable and disable relevant apache2 modules
    c.run(
        "sudo a2enmod headers rewrite mpm_event http2 mime proxy proxy_fcgi "
        "setenvif alias dir env ssl proxy_http proxy_wstunnel"
    )
    c.run("sudo a2dismod mpm_prefork")
    c.run("sudo a2ensite nextcloud.conf")

    # increase memcached memory (see config file)
    old = dedent("""
    # Note that the daemon will grow to this size, but does not start out holding this much
    # memory

    """).lstrip("\n")
    new = dedent(f"""
    # Note that the daemon will grow to this size, but does not start out holding this much
    # memory

    """).lstrip("\n")

    c.multi_edit_file("/etc/memcached.conf", [("-m 64", f"-m {config('memcached_memory')}")])

    pool_conf_fpath = f"/etc/php/{PHP_VERSION}/fpm/pool.d/www.conf"
    replacements = [
        ("max_children = 5", "max_children = 80"),
        ("start_servers = 2", "start_servers = 20"),
        ("min_spare_servers = 1", "min_spare_servers = 20"),
        ("max_spare_servers = 3", "max_spare_servers = 60"),

        (";env[HOSTNAME] = $HOSTNAME", "env[HOSTNAME] = $HOSTNAME"),
        (";env[PATH] = /usr/local/bin:/usr/bin:/bin", "env[PATH] = /usr/local/bin:/usr/bin:/bin",),
        (";env[TMP] = /tmp", "env[TMP] = /tmp"),
        (";env[TMPDIR] = /tmp", "env[TMPDIR] = /tmp"),
        (";env[TEMP] = /tmp", "env[TEMP] = /tmp"),
    ]
    c.multi_edit_file(pool_conf_fpath, replacements)

    php_ini_fpath = f"/etc/php/{PHP_VERSION}/fpm/php.ini"
    replacements = [
        ("memory_limit = 128M", "memory_limit = 1024M"),
        ("post_max_size = 8M", "post_max_size = 512M"),
        ("upload_max_filesize = 2M", "upload_max_filesize = 1024M"),
        (";opcache.enable=1", "opcache.enable=1"),
        (";opcache.memory_consumption=128", "opcache.memory_consumption=1024"),
        (";opcache.interned_strings_buffer=8", "opcache.interned_strings_buffer=64"),
        (";opcache.max_accelerated_files=10000", "opcache.max_accelerated_files=150000"),
        (";opcache.max_wasted_percentage=5", "opcache.max_wasted_percentage=15"),
        (";opcache.revalidate_freq=2", "opcache.revalidate_freq=60"),
        (";opcache.save_comments=1", "opcache.save_comments=1"),
    ]

    # add special instructions at the end of the section (no commented template available)
    # note that [curl] starts the next section

    old = dedent("""
    ;opcache.lockfile_path=/tmp

    [curl]
    """).lstrip("\n")
    new = dedent(f"""
    ;opcache.lockfile_path=/tmp

    opcache.jit=1255
    opcache.jit_buffer_size=256M

    [curl]
    """).lstrip("\n")

    replacements.append((old, new))
    c.multi_edit_file(php_ini_fpath, replacements)


def nc_prep03(c: du.StateConnection):

    user = config("sql_user")
    password = config("sql_password")

    sql_commands = [
        f"CREATE USER '{user}'@'localhost' IDENTIFIED BY '{password}';",
        f"CREATE DATABASE IF NOT EXISTS nextcloud CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;",
        f"GRANT ALL PRIVILEGES ON nextcloud.* TO '{config('sql_user')}'@'localhost';",
        "FLUSH PRIVILEGES;",
    ]

    for cmd in sql_commands:
        c.run(f"mysql --execute \"{cmd}\"")

def download_and_unzip_nc(c: du.StateConnection):

    c.chdir("/var/www")
    c.run(f"wget {config('nc_release_file_url')}")

    # unpack tar.bz2 file
    c.run("tar xjf nextcloud*.tar.bz2")
    c.run("chown -R www-data:www-data /var/www/nextcloud")

    # disable default apache2 demo page
    c.run("a2dissite 000-default.conf")
    c.run("a2ensite nextcloud.conf")

    c.run("systemctl restart apache2")
    c.run("systemctl restart memcached")
    c.run(f"systemctl restart php{PHP_VERSION}-fpm")


def initial_nc_config(c):

    occ_base_cmd = "sudo -u www-data php /var/www/nextcloud/occ"
    cmd1 = dedent(f"""
    {occ_base_cmd} maintenance:install \
    --database "mysql" \
    --database-name "nextcloud" \
    --database-user "{config("sql_user")}" \
    --database-pass "{config("sql_password")}" \
    --admin-user "{config("nc_admin_user")}" \
    --admin-pass "{config("nc_admin_pw")}" \
    --data-dir "/var/www/nextcloud/data"
    """)
    c.run(cmd1)
    cmd2 = f'{occ_base_cmd} config:system:set trusted_domains 1 --value="{config("server_name")}"'
    c.run(cmd2)

    # this url will be used e.g. in automatically generated emails
    cmd3 = f'{occ_base_cmd} config:system:set overwrite.cli.url --value="https://{config("server_name")}"'

    c.run(cmd3)

    # activate memcache (recommendation from the video)
    pairs = [
        ("memcache.local", r"\OC\Memcache\APCu"),
        ("memcache.distributed", r"\OC\Memcache\Memcached"),
        ("memcache.locking", r"\OC\Memcache\Memcached"),
    ]
    for varname, value in pairs:
        # note that if you edit the config file (display it with `cat config.php`) the backslashes appear
        # doubled (e.g. "\\OC\\Memcache\\Memcached" representing "\OC\Memcache\Memcached")
        cmd4 = f"{occ_base_cmd} config:system:set {varname} --value='{value}'"
        c.run(cmd4)


if 0:
    # this is needed when run nc prep from scratch because it is missing in my test-image
    c.run(f"apt install --assume-yes rsync")
    c.run(f"mkdir -p ~/.config/mc")
    c.rsync_upload("config_files/mc/", "~/.config/mc", "remote")

    # this is the actual nextcloud installation:
    nc_prep01(c)
    nc_prep02(c)
    nc_prep03(c)
    download_and_unzip_nc(c)

    # at this point the tutorial video continues via browser
    # we need to automate this (using `sudo -u www-data php /var/www/nextcloud/occ ...`)
    initial_nc_config(c)

IPS()
