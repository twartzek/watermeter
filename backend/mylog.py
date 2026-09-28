import logging
import os
from dotenv import dotenv_values
from logging.handlers import RotatingFileHandler

config = dotenv_values("watermeter/.env")


class Logger:
    level_relations = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warning": logging.WARNING,
        "error": logging.ERROR,
        "crit": logging.CRITICAL,
    }  # relationship mapping

    def __init__(
        self,
        name,
        level="debug",
        maxBytes=10 * 1024 * 1024,
        backCount=3,
        fmt="%(asctime)s - [line:%(lineno)d] - %(levelname)s: %(message)s",
    ):
        self.logger = logging.getLogger(name)
        format_str = logging.Formatter(fmt)  # Setting the log format
        self.logger.setLevel(self.level_relations.get(level))  # Setting the log level
        console_handler = logging.StreamHandler()  # on-screen output
        console_handler.setFormatter(format_str)  # Setting the format
        logpath = os.path.join(config["log"], name+".log")
        th = RotatingFileHandler(
            filename=logpath,  backupCount=backCount, encoding="utf-8"
        )  
        th.setFormatter(format_str)  # Setting the format
        self.logger.addHandler(console_handler)  # Add the object to the logger
        self.logger.addHandler(th)
