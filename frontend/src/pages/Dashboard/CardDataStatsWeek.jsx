import React from "react";
import CardDataStats from "../../components/CardDataStats";
import useSWR from "swr";
import { BsCalendarWeek } from "react-icons/bs";
import { DateTime } from "luxon";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import CardDataStatsLoading from "../../components/CardDataStatsLoading";

const fetcher = async (url) => fetch(url).then((res) => res.json());

function CardDataStatsWeek() {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);

  // This week data
  const startDateString = DateTime.local({ zone: "utc" })
    .startOf("week")
    .toISO();
  const endDateString = DateTime.local({ zone: "utc" }).endOf("week").toISO();

  const url =
    "http://" +
    hostname +
    ":8000/api/v1/consumptionbetween?start=" +
    startDateString +
    "&end=" +
    endDateString;

  // Last week
  const startDateStringLastWeek = DateTime.local({ zone: "utc" })
    .minus({ weeks: 1 })
    .startOf("week")
    .toISO();
  const endDateStringLastWeek = DateTime.local({ zone: "utc" })
    .minus({ weeks: 1 })
    .endOf("week")
    .toISO();

  const urlLastWeek =
    "http://" +
    hostname +
    ":8000/api/v1/consumptionbetween?start=" +
    startDateStringLastWeek +
    "&end=" +
    endDateStringLastWeek;

  //  Fetch data
  const {
    data: dataThisWeek,
    error: errorThisWeek,
    isLoading: isLoadingThisWeek,
  } = useSWR(url, fetcher, {
    refreshInterval: 60000,
  });

  const {
    data: dataLastWeek,
    error: errorLastWeek,
    isLoading: isLoadingLastWeek,
  } = useSWR(urlLastWeek, fetcher, {
    refreshInterval: 60000,
  });

  if (isLoadingLastWeek || isLoadingThisWeek) return <CardDataStatsLoading />;

  if (errorLastWeek || errorThisWeek)
    return (
      <div className="rounded-sm border border-stroke bg-white py-6 px-7.5 shadow-default dark:border-strokedark dark:bg-boxdark">
        {t("apierror")}
      </div>
    );

  let consumptionLastWeek = 0;
  let consumptionThisWeek = null;
  let rate = 0;

  // Calculate data. consumption is already corrected server-side for any
  // confirmed meter replacement in the range (see db.getConsumptionBetween)
  // -- computing it client-side from raw readings would treat a
  // replacement's reset-to-zero as a large negative consumption.
  if (dataLastWeek.consumption != null) {
    consumptionLastWeek = dataLastWeek.consumption;
  }

  if (dataThisWeek.consumption != null) {
    consumptionThisWeek = dataThisWeek.consumption;
  }

  if (!consumptionThisWeek) {
    rate = null;
  } else {
    rate =
      ((consumptionThisWeek - consumptionLastWeek) / consumptionThisWeek) * 100;
  }

  return (
    <CardDataStats
      title={t("this week")}
      total={
        consumptionThisWeek == null
          ? t("nodata")
          : consumptionThisWeek.toFixed(2) + " m³"
      }
      rate={rate ? rate?.toFixed(0) + "%" : ""}
      levelUp={rate > 0}
      levelDown={rate < 0}
    >
      <BsCalendarWeek
        className="fill-primary dark:fill-white"
        style={{ fontSize: "1.5em" }}
      />
    </CardDataStats>
  );
}

export default CardDataStatsWeek;
