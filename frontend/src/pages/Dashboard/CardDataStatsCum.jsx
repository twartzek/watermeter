import React from "react";
import CardDataStats from "../../components/CardDataStats";
import useSWR from "swr";
import { BsSpeedometer } from "react-icons/bs";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import { DateTime } from "luxon";
import CardDataStatsLoading from "../../components/CardDataStatsLoading";

const fetcher = async (url) => fetch(url).then((res) => res.json());

function CardDataStatsCum() {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);

  // /readings/lastsuccessful instead of /readings/last: always returns the
  // last *successful* value (regardless of developer mode) -- a single
  // failed measurement attempt (e.g. a watchdog reboot mid-inference)
  // should not show "no data" here as long as there was a valid value
  // before. isStale flags when this last successful value is already
  // older, so a days-old value doesn't silently pass as current (see
  // restapi.py).
  const url = "http://" + hostname + ":8000/api/v1/readings/lastsuccessful";

  const { data, error, isLoading } = useSWR(url, fetcher, {
    refreshInterval: 60000,
  });

  if (isLoading) return <CardDataStatsLoading />;

  if (error)
    return (
      <div className="rounded-sm border border-stroke bg-white py-6 px-7.5 shadow-default dark:border-strokedark dark:bg-boxdark">
        {t("measurementinprogress")}
      </div>
    );

  // This tile shows the current OCR-detected meter reading -- the raw value
  // (data.totalconsumption), as it can be read off the meter. The total
  // consumption carried forward across meter replacements
  // (data.cumulativeTotal) has its own tile, see
  // CardDataStatsCumulative.jsx.
  //
  // id === null means there is no successful reading at all yet (fresh
  // install / DB reset) -- that is "no data".
  const noDataYet = data.id == null;
  return (
    <CardDataStats
      title={t("cumtoday")}
      total={noDataYet ? t("nodata") : data.totalconsumption.toFixed(2) + " m³"}
      subtitle={
        !noDataYet &&
        t(data.isStale ? "stalereading" : "asof", {
          datetime: DateTime.fromISO(data.datetime).toLocaleString(
            DateTime.DATETIME_MED
          ),
        })
      }
      stale={!noDataYet && data.isStale}
    >
      <BsSpeedometer
        className="fill-primary dark:fill-white"
        style={{ fontSize: "1.5em" }}
      />
    </CardDataStats>
  );
}

export default CardDataStatsCum;
