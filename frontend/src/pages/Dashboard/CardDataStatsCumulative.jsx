import React from "react";
import CardDataStats from "../../components/CardDataStats";
import useSWR from "swr";
import { BsWater } from "react-icons/bs";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import { DateTime } from "luxon";
import CardDataStatsLoading from "../../components/CardDataStatsLoading";

const fetcher = async (url) => fetch(url).then((res) => res.json());

// Total consumption since commissioning: the meter reading carried forward
// across all confirmed meter replacements (data.cumulativeTotal, see
// db.getCumulativeTotal), unlike CardDataStatsCum, which shows the current
// OCR-detected reading of the most recently installed meter (which starts
// again at ~0 after a replacement).
function CardDataStatsCumulative() {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);

  // /readings/lastsuccessful instead of /readings/last: see the comment in
  // CardDataStatsCum.jsx -- a single failed measurement attempt should not
  // show "no data" here as long as there was a valid value before. isStale
  // marks outdated values instead of silently showing them as current.
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

  // id === null means there is no successful reading at all yet (fresh
  // install / DB reset) -- that is "no data".
  const noDataYet = data.id == null;
  return (
    <CardDataStats
      title={t("cumtotal")}
      total={noDataYet ? t("nodata") : data.cumulativeTotal.toFixed(2) + " m³"}
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
      <BsWater
        className="fill-primary dark:fill-white"
        style={{ fontSize: "1.5em" }}
      />
    </CardDataStats>
  );
}

export default CardDataStatsCumulative;
