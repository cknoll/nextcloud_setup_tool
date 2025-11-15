import time
import os
import sys
import os
from os.path import join as pjoin

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


"""
This script serves to create a golden image for a nextcloud installation
"""

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

# this is the root dir of the project (where setup.py lies)
# if you maintain more than one instance (and deploy.py lives outside the project dir, this has to change)
project_src_path = os.path.dirname(du.get_dir_of_this_file())

temp_workdir = pjoin(du.get_dir_of_this_file(), "tmp_workdir")  # this will be deleted/overwritten

# TODO: adapt if needed
# assert os.path.isfile(os.path.join(project_src_path, "manage.py"))


du.argparser.add_argument(
    "--debug", help="start debug interactive mode (IPS), then exit", action="store_true"
)

# always pass remote as argument (reason: legacy)
# TODO: adapt deployment tools such that this is not needed

# assumes call starts with with `python deployment/deploy.py`
args = du.parse_args(sys.argv[1:] + ["remote"])


final_msg = f"Deployment script {du.bgreen('done')}."

if not args.target == "remote":
    raise NotImplementedError("local deployment is not supported by this script")

time.sleep(1)


# ensure clean workdir
os.system(f"rm -rf {temp_workdir}")
os.makedirs(temp_workdir)

c = du.StateConnection(remote, user=user, target=args.target)

c.run(f"echo hello new vm with os:")
# get name of linux distribution
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
    # trailing slash at source is important
    c.rsync_upload("config_files/mc/", "~/.config/mc", "remote")


IPS()
