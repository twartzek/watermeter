import React from "react";
import CardDataStats from "../../components/CardDataStats";
import useSWR from "swr";
import { BsWater } from "react-icons/bs";
import { useTranslation } from "react-i18next";
import { useState } from "react";
import { DateTime } from "luxon";
import CardDataStatsLoading from "../../components/CardDataStatsLoading";

const fetcher = async (url) => fetch(url).then((res) => res.json());

// Gesamtverbrauch seit Inbetriebnahme: der ueber alle bestaetigten
// Zaehlertausche hinweg fortgeschriebene Zaehlerstand (data.cumulativeTotal,
// siehe db.getCumulativeTotal), im Gegensatz zu CardDataStatsCum, die den
// per OCR erkannten aktuellen Zaehlerstand des zuletzt verbauten Zaehlers
// zeigt (der nach einem Tausch wieder bei ~0 anfaengt).
function CardDataStatsCumulative() {
  const { t, i18n } = useTranslation();
  const [hostname] = useState(() => window.location.hostname);

  // /readings/lastsuccessful statt /readings/last: siehe Kommentar in
  // CardDataStatsCum.jsx -- ein einzelner fehlgeschlagener Messversuch soll
  // hier nicht "Keine Daten" zeigen, solange zuvor ein gueltiger Wert
  // vorlag. isStale markiert veraltete Werte statt sie unbemerkt als
  // aktuell anzuzeigen.
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

  // id === null bedeutet: es gibt noch gar keine erfolgreiche Reading
  // (frische Installation / DB-Reset) -- das ist "keine Daten".
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
