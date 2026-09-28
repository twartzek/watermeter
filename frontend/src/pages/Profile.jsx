import Breadcrumb from "../components/Breadcrumbs/Breadcrumb";
import VersionBadge from "../components/VersionBadge";
import { useTranslation } from "react-i18next";

const Profile = () => {
  const { t } = useTranslation();
  return (
    <>
      <Breadcrumb pageName={t("about")} />

      <div className="rounded-sm border border-stroke bg-white shadow-default dark:border-strokedark dark:bg-boxdark">
        <div className="p-7">
          <h3 className="mb-1.5 text-2xl font-semibold text-black dark:text-white">
            WatermeterAI NextGen 💧
          </h3>
          <p className="mb-5.5 font-medium">
            {t("aboutsubtitle")}
          </p>

          <p className="mb-5.5 max-w-180">{t("aboutdescription")}</p>

          <div className="mb-5.5">
            <h4 className="mb-1.5 font-semibold text-black dark:text-white">
              {t("aboutsource")}
            </h4>
            <a
              href="https://github.com/twartzek/watermeter"
              target="_blank"
              rel="noreferrer"
              className="text-primary hover:underline"
            >
              github.com/twartzek/watermeter
            </a>
          </div>

          <div>
            <h4 className="mb-1.5 font-semibold text-black dark:text-white">
              {t("aboutversion")}
            </h4>
            <VersionBadge />
          </div>
        </div>
      </div>
    </>
  );
};

export default Profile;
