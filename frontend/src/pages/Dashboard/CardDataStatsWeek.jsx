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

  // Rolling 7-day window instead of the calendar week: a calendar week is
  // only comparable to the previous one once it is over, a rolling window
  // always covers the same span. The end is rounded up to the full hour so
  // the SWR key stays stable between renders; readings newer than "now"
  // don't exist, so the extra minutes don't change the result.
  const windowEnd = DateTime.local({ zone: "utc" }).endOf("hour");
  const windowStart = windowEnd.minus({ days: 7 });
  const previousWindowStart = windowStart.minus({ days: 7 });

  const url =
    "http://" +
    hostname +
    ":8000/api/v1/consumptionbetween?start=" +
    windowStart.toISO() +
    "&end=" +
    windowEnd.toISO();

  // The 7 days before that
  const urlLastWeek =
    "http://" +
    hostname +
    ":8000/api/v1/consumptionbetween?start=" +
    previousWindowStart.toISO() +
    "&end=" +
    windowStart.toISO();

  //  Fetch data
  const {
    data: dataThisWeek,
    error: errorThisWeek,
    isLoading: isLoadingThisWeek,
  } = useSWR(url, fetcher, {
    refreshInterval: 60000,
    keepPreviousData: true,
  });

  const {
    data: dataLastWeek,
    error: errorLastWeek,
    isLoading: isLoadingLastWeek,
  } = useSWR(urlLastWeek, fetcher, {
    refreshInterval: 60000,
    keepPreviousData: true,
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

  // Change relative to the previous window. Without a previous value there
  // is nothing to compare against, so no rate is shown.
  if (consumptionThisWeek == null || !consumptionLastWeek) {
    rate = null;
  } else {
    rate =
      ((consumptionThisWeek - consumptionLastWeek) / consumptionLastWeek) * 100;
  }

  return (
    <CardDataStats
      title={t("last7days")}
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
