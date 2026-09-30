import json
import os
from dotenv import dotenv_values

config = dotenv_values("watermeter/.env")  

SETTINGSPATH = config["settingspath"]


def readSettings():
    if os.path.exists(SETTINGSPATH) and os.path.getsize(SETTINGSPATH) > 0:
        with open(SETTINGSPATH, 'r') as file:
            settings = json.load(file)
        # Backfill for settings files written before developer mode
        # existed.
        settings.setdefault("developerMode", False)
        return settings
    else:
        with open(SETTINGSPATH, 'w') as file:
            settings = {"mqtt":{"broker":"192.168.1.2","port":1883,"username":"a","password":"b"},"smtp":{"server":"smtp.strato.com","port":465,"sender":"dummy@mail.com","recipient":"dummy@mail.com","password":"secret"},"developerMode":False}
            json.dump(settings, file)
        return settings

def writeSettings(settings: str):
    with open(SETTINGSPATH, 'w') as file:
        file.write(settings)


if __name__ == "__main__":
    settings = readSettings()
    print(settings)