This script assumes a local Linux system (other OS not tested) with `rsync` and python (>=3.11) installed locally.


- Install dependencies:
    - `python -m pip install --user -r requirements.txt`
- Create you config file:
    - copy `config-example.toml` to `config.toml` and insert your values
- Unlock ssh key (e.g. for 10 minutes):
    - `eval $(ssh-agent); ssh-add -t 10m`
    - necessary because the script executes multiple `ssh` and `rsync` commands
- Run the script (you probably want to edit it before):
    - `python ubuntu24.04_v1.py`
