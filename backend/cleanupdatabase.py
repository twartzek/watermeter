from db import thin_out_old_images
from mylog import Logger

logger = Logger("cleanupdatabase")

if __name__ == "__main__":
    logger.logger.info("Started cleanupdatabase main")
    thin_out_old_images()
