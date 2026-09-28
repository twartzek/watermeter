from db import delete_old_readings
from mylog import Logger

logger = Logger("cleanupdatabase")

if __name__ == "__main__":
    logger.logger.info("Started cleanupdatabase main")
    delete_old_readings()
