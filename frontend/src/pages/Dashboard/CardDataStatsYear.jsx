import React from "react";
import CardDataStats from "../../components/CardDataStats";
import useSWR from "swr";
import { BsDroplet } from "react-icons/bs";
import { DateTime } from "luxon";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import CardDataStatsLoading from "../../components/CardDataStatsLoading";

const fetcher = async (url) => fetch(url).then((res) => res.json());

function CardDataStatsYear() {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);
  const thisYear = DateTime.local({ zone: "utc" }).year;

  const urlperYear = "http://" + hostname + ":8000/api/v1/consumptionperyear";

  const { data, error, isLoading } = useSWR(urlperYear, fetcher, {
    refreshInterval: 60000,
  });

  if (isLoading) return <CardDataStatsLoading />;

  if (error)
    return (
      <div className="rounded-sm border border-stroke bg-white py-6 px-7.5 shadow-default dark:border-strokedark dark:bg-boxdark">
        {t("apierror")}
      </div>
    );

  const itemThisYear = data.find((item) => item.year === thisYear);
  const consumptionThisYear = itemThisYear?.consumption;
  const consumptionRate = itemThisYear?.rate;

  return (
    <CardDataStats
      title={t("this year")}
      total={
        consumptionThisYear == null
          ? t("nodata")
          : consumptionThisYear.toFixed(2) + " m³"
      }
      rate={consumptionRate ? consumptionRate?.toFixed(0) + "%" : ""}
      levelUp={consumptionRate > 0}
      levelDown={consumptionRate < 0}
    >
      <BsDroplet
        className="fill-primary dark:fill-white"
        style={{ fontSize: "1.5em" }}
      />
    </CardDataStats>
  );
}

export default CardDataStatsYear;
