from db import thin_out_old_images
from mylog import Logger

logger = Logger("cleanupdatabase")

if __name__ == "__main__":
    logger.logger.info("Started cleanupdatabase main")
    thinned, orphaned = thin_out_old_images()
    logger.logger.info(
        f"Finished cleanupdatabase main: removed {thinned} old photo(s) "
        f"and {orphaned} orphaned photo(s)"
    )
