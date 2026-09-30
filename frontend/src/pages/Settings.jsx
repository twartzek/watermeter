import Breadcrumb from "../components/Breadcrumbs/Breadcrumb";
import { MdOutlineEmail } from "react-icons/md";
import { BsPerson } from "react-icons/bs";
import { useTranslation } from "react-i18next";
import { BsServer } from "react-icons/bs";
import { FaRegUser } from "react-icons/fa6";
import { useForm } from "react-hook-form";
import useSWR from "swr";
import { useState, useRef } from "react";
import { enqueueSnackbar } from "notistack";

const fetcher = (...args) => fetch(...args).then((res) => res.json());

const Settings = () => {
  const { t, i18n } = useTranslation();
  const [emailcheck, setEmailcheck] = useState(false);
  const [hostname] = useState(() => window.location.hostname);
  const [developerMode, setDeveloperMode] = useState(false);
  const developerModeInitialized = useRef(false);
  const [confirmingMeterReplacement, setConfirmingMeterReplacement] = useState(false);
  const [deletingNotifications, setDeletingNotifications] = useState(false);
  const {
    register,
    handleSubmit,
    setValue,
    watch,
    formState: { errors },
  } = useForm();

  const url = "http://" + hostname + ":8000/api/v1/settings";
  const { data, error, isLoading } = useSWR(url, fetcher, {});

  if (isLoading) return "Loading...";
  if (error)
    return (
      <div className="rounded-sm border border-stroke bg-white py-6 px-7.5 shadow-default dark:border-strokedark dark:bg-boxdark">
        {t("measurementinprogress")}
      </div>
    );

  const onSubmit = async (formData) => {
    try {
      data.mqtt.broker = formData.broker;
      data.mqtt.port = formData.port;
      data.mqtt.username = formData.username;
      data.mqtt.password = formData.password;
      data.smtp.sender = formData.smtpsender;
      data.smtp.recipient = formData.smtprecipient;
      data.smtp.server = formData.smtpserver;
      data.smtp.port = formData.smtpport;
      data.smtp.password = formData.smtppassword;
      data.developerMode = developerMode;
      const response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(data),
      });

      if (!response.ok) {
        enqueueSnackbar(t("errorpost"), { variant: "error" });
      } else {
        enqueueSnackbar(t("successpost"), { variant: "success" });
      }
    } catch (error) {
      enqueueSnackbar(t("errorpost") + " " + error, { variant: "error" });
    }
  };

  const handleReset = () => {
    setValue("broker", data.mqtt.broker);
    setValue("port", data.mqtt.port);
    setValue("username", data.mqtt.username);
    setValue("password", data.mqtt.password);
    setValue("smtpsender", data.smtp.sender);
    setValue("smtprecipient", data.smtp.recipient);
    setValue("smtpserver", data.smtp.server);
    setValue("smtpport", data.smtp.port);
    setValue("smtppassword", data.smtp.password);
    setDeveloperMode(!!data.developerMode);
  };

  const handleConfirmMeterReplacement = async () => {
    if (!window.confirm(t("meterreplacementdescription"))) {
      return;
    }
    setConfirmingMeterReplacement(true);
    try {
      const response = await fetch(
        "http://" + hostname + ":8000/api/v1/meterreplacement/confirm",
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
        }
      );
      if (!response.ok) {
        enqueueSnackbar(t("meterreplacementconfirmerror"), { variant: "error" });
      } else {
        enqueueSnackbar(t("meterreplacementconfirmsuccess"), { variant: "success" });
      }
    } catch (error) {
      enqueueSnackbar(t("meterreplacementconfirmerror") + " " + error, {
        variant: "error",
      });
    } finally {
      setConfirmingMeterReplacement(false);
    }
  };

  const handleDeleteAllNotifications = async () => {
    if (!window.confirm(t("deletenotificationsconfirm"))) {
      return;
    }
    setDeletingNotifications(true);
    try {
      const response = await fetch(
        "http://" + hostname + ":8000/api/v1/notifications",
        {
          method: "DELETE",
          headers: {
            "Content-Type": "application/json",
          },
        }
      );
      if (!response.ok) {
        enqueueSnackbar(t("deletenotificationserror"), { variant: "error" });
      } else {
        enqueueSnackbar(t("deletenotificationssuccess"), { variant: "success" });
      }
    } catch (error) {
      enqueueSnackbar(t("deletenotificationserror") + " " + error, {
        variant: "error",
      });
    } finally {
      setDeletingNotifications(false);
    }
  };

  const handleCheckEmail = async () => {
    setEmailcheck(true);
    try {
      const response = await fetch(
        "http://" + hostname + ":8000/api/v1/checksmtp",
        {
          method: "GET",
          headers: {
            "Content-Type": "application/json",
          },
        }
      );
      console.log(response);
      if (!response.ok) {
        enqueueSnackbar(t("smtpcheckfailed"), { variant: "error" });
      } else {
        enqueueSnackbar(t("smtpchecksuccess"), { variant: "success" });
      }
    } catch (error) {
      enqueueSnackbar(t("smtpcheckfailed") + " " + error, { variant: "error" });
    } finally {
      setEmailcheck(false);
    }
  };

  setValue("broker", data.mqtt.broker);
  setValue("port", data.mqtt.port);
  setValue("username", data.mqtt.username);
  setValue("password", data.mqtt.password);
  setValue("smtpsender", data.smtp.sender);
  setValue("smtprecipient", data.smtp.recipient);
  setValue("smtpserver", data.smtp.server);
  setValue("smtpport", data.smtp.port);
  setValue("smtppassword", data.smtp.password);

  // Only take this over from the server data once, so a later re-render
  // (e.g. after saving) doesn't overwrite the user's unsaved toggle.
  if (!developerModeInitialized.current) {
    developerModeInitialized.current = true;
    if (!!data.developerMode !== developerMode) {
      setDeveloperMode(!!data.developerMode);
    }
  }

  return (
    <>
      <div className="mx-auto max-w-270">
        <Breadcrumb pageName={t("settings")} />

        <div className="grid grid-cols-5 gap-8">
          <div className="col-span-5 xl:col-span-3">
            <div className="rounded-sm border border-stroke bg-white shadow-default dark:border-strokedark dark:bg-boxdark">
              <form onSubmit={handleSubmit(onSubmit)}>
                <div className="border-b border-stroke py-4 px-7 dark:border-strokedark">
                  <h3 className="font-medium text-black dark:text-white">
                    MQTT
                  </h3>
                </div>

                <div className="p-7">
                  <div className="mb-5.5 flex flex-col gap-5.5 sm:flex-row">
                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="broker"
                      >
                        {t("broker")}
                      </label>
                      <div className="relative">
                        <span className="absolute left-4.5 top-4">
                          <BsServer />
                        </span>
                        <input
                          className="w-full rounded border border-stroke bg-gray py-3 pl-11.5 pr-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                          type="text"
                          name="broker"
                          id="broker"
                          placeholder="mybroker.de"
                          defaultValue="mybroker.de"
                          {...register("broker", { required: true })}
                        />
                        {errors.broker && (
                          <span className="text-danger">
                            {t("thisfieldrequired")}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="port"
                      >
                        {t("port")}
                      </label>
                      <input
                        className="w-full rounded border border-stroke bg-gray py-3 px-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                        type="number"
                        name="port"
                        id="port"
                        placeholder="1883"
                        defaultValue="1883"
                        {...register("port", { required: true })}
                      />
                    </div>
                  </div>

                  <div className="mb-5.5 flex flex-col gap-5.5 sm:flex-row">
                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="user"
                      >
                        {t("username")}
                      </label>
                      <div className="relative">
                        <span className="absolute left-4.5 top-4">
                          <FaRegUser />
                        </span>
                        <input
                          className="w-full rounded border border-stroke bg-gray py-3 pl-11.5 pr-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                          type="text"
                          name="mqtt user"
                          id="user"
                          placeholder={t("username")}
                          defaultValue={t("username")}
                          {...register("username", { required: true })}
                        />
                        {errors.broker && (
                          <span className="text-danger">
                            {t("thisfieldrequired")}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="password"
                      >
                        {t("password")}
                      </label>
                      <input
                        className="w-full rounded border border-stroke bg-gray py-3 px-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                        type="password"
                        name="password"
                        id="password"
                        placeholder={t("password")}
                        {...register("password", { required: true })}
                      />
                    </div>
                  </div>
                </div>
                <div className="border-b border-stroke py-4 px-7 dark:border-strokedark">
                  <h3 className="font-medium text-black dark:text-white">
                    SMTP E-Mail
                  </h3>
                </div>

                <div className="p-7">
                  <div className="mb-5.5 flex flex-col gap-5.5 sm:flex-row">
                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="smtpserver"
                      >
                        SMTP Server
                      </label>
                      <div className="relative">
                        <span className="absolute left-4.5 top-4">
                          <BsServer />
                        </span>
                        <input
                          className="w-full rounded border border-stroke bg-gray py-3 pl-11.5 pr-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                          type="text"
                          name="smtpserver"
                          id="smtpserver"
                          placeholder="smtp.strato.com"
                          defaultValue="smtp.strato.com"
                          {...register("smtpserver", { required: true })}
                        />
                        {errors.broker && (
                          <span className="text-danger">
                            {t("thisfieldrequired")}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="smtpport"
                      >
                        {t("port")}
                      </label>
                      <input
                        className="w-full rounded border border-stroke bg-gray py-3 px-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                        type="number"
                        name="smtpport"
                        id="smtpport"
                        placeholder="465"
                        defaultValue="465"
                        {...register("smtpport", { required: true })}
                      />
                    </div>
                  </div>

                  <div className="mb-5.5 flex flex-col gap-5.5 sm:flex-row">
                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="smtpsender"
                      >
                        {t("sender")}
                      </label>
                      <div className="relative">
                        <span className="absolute left-4.5 top-4">
                          <MdOutlineEmail />
                        </span>
                        <input
                          className="w-full rounded border border-stroke bg-gray py-3 pl-11.5 pr-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                          type="email"
                          name="smtpsender"
                          id="smtpsender"
                          placeholder="watermeterai@yourmail.com"
                          defaultValue="watermeterai@yourmail.com"
                          {...register("smtpsender", {
                            required: true,
                          })}
                        />
                        {errors.broker && (
                          <span className="text-danger">
                            {t("thisfieldrequired")}
                          </span>
                        )}
                      </div>
                    </div>

                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="smtppassword"
                      >
                        {t("password")}
                      </label>
                      <input
                        className="w-full rounded border border-stroke bg-gray py-3 px-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                        type="password"
                        name="smtppassword"
                        id="smtppassword"
                        placeholder={t("password")}
                        {...register("smtppassword", { required: true })}
                      />
                    </div>
                  </div>
                  <div className="mb-5.5 flex flex-col gap-5.5 sm:flex-row">
                    <div className="w-full sm:w-1/2">
                      <label
                        className="mb-3 block text-sm font-medium text-black dark:text-white"
                        htmlFor="smtprecipient"
                      >
                        {t("recipient")}
                      </label>
                      <div className="relative">
                        <span className="absolute left-4.5 top-4">
                          <MdOutlineEmail />
                        </span>
                        <input
                          className="w-full rounded border border-stroke bg-gray py-3 pl-11.5 pr-4.5 text-black focus:border-primary focus-visible:outline-none dark:border-strokedark dark:bg-meta-4 dark:text-white dark:focus:border-primary"
                          type="email"
                          name="smtprecipient"
                          id="smtprecipient"
                          placeholder="watermeterai@yourmail.com"
                          defaultValue="watermeterai@yourmail.com"
                          {...register("smtprecipient", {
                            required: true,
                          })}
                        />
                        {errors.broker && (
                          <span className="text-danger">
                            {t("thisfieldrequired")}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                <div className="border-b border-stroke py-4 px-7 dark:border-strokedark">
                  <h3 className="font-medium text-black dark:text-white">
                    {t("meterreplacement")}
                  </h3>
                </div>

                <div className="p-7">
                  <p className="mb-4 text-sm text-body dark:text-bodydark">
                    {t("meterreplacementdescription")}
                  </p>
                  <button
                    className="flex justify-center rounded border border-stroke py-2 px-6 font-medium text-black hover:shadow-1 dark:border-strokedark dark:text-white"
                    type="button"
                    onClick={handleConfirmMeterReplacement}
                    disabled={confirmingMeterReplacement}
                  >
                    <svg
                      className={
                        confirmingMeterReplacement
                          ? "mr-3 size-5 animate-spin rounded-full border-2 border-solid  border-t-transparent"
                          : "hidden"
                      }
                      viewBox="0 0 24 24"
                    ></svg>
                    {t("confirmmeterreplacement")}
                  </button>
                </div>

                <div className="border-b border-stroke py-4 px-7 dark:border-strokedark">
                  <h3 className="font-medium text-black dark:text-white">
                    {t("developer")}
                  </h3>
                </div>

                <div className="p-7">
                  <div className="mb-5.5 flex flex-row items-center gap-3">
                    <label
                      htmlFor="developerMode"
                      className="flex cursor-pointer select-none items-center"
                    >
                      <div className="relative">
                        <input
                          type="checkbox"
                          id="developerMode"
                          className="sr-only"
                          checked={developerMode}
                          onChange={() => setDeveloperMode(!developerMode)}
                        />
                        <div className="block h-8 w-14 rounded-full bg-meta-9 dark:bg-[#5A616B]"></div>
                        <div
                          className={`absolute left-1 top-1 h-6 w-6 rounded-full bg-white transition ${
                            developerMode &&
                            "!right-1 !translate-x-full !bg-primary dark:!bg-white"
                          }`}
                        ></div>
                      </div>
                    </label>
                    <span className="text-sm font-medium text-black dark:text-white">
                      {t("developermode")}
                    </span>
                  </div>
                  <p className="text-sm text-body dark:text-bodydark">
                    {t("developermodedescription")}
                  </p>

                  {developerMode && (
                    <div className="mt-5.5">
                      <p className="mb-4 text-sm text-body dark:text-bodydark">
                        {t("deletenotificationsdescription")}
                      </p>
                      <button
                        className="flex justify-center rounded border border-danger py-2 px-6 font-medium text-danger hover:shadow-1"
                        type="button"
                        onClick={handleDeleteAllNotifications}
                        disabled={deletingNotifications}
                      >
                        <svg
                          className={
                            deletingNotifications
                              ? "mr-3 size-5 animate-spin rounded-full border-2 border-solid  border-t-transparent"
                              : "hidden"
                          }
                          viewBox="0 0 24 24"
                        ></svg>
                        {t("deleteallnotifications")}
                      </button>
                    </div>
                  )}
                </div>

                <div className="flex justify-end gap-4.5 p-7 pt-0">
                    <button
                      className="flex justify-center rounded border border-stroke py-2 px-6 font-medium text-black hover:shadow-1 dark:border-strokedark dark:text-white"
                      type="button"
                      onClick={handleReset}
                    >
                      {t("reset")}
                    </button>
                    <button
                      className="flex justify-center rounded border border-stroke py-2 px-6 font-medium text-black hover:shadow-1 dark:border-strokedark dark:text-white"
                      type="button"
                      onClick={handleCheckEmail}
                      disabled={emailcheck}
                    >
                      <svg
                        className={
                          emailcheck
                            ? "mr-3 size-5 animate-spin rounded-full border-2 border-solid  border-t-transparent"
                            : "hidden"
                        }
                        viewBox="0 0 24 24"
                      ></svg>
                      Check E-Mail
                    </button>
                    <button
                      className="flex justify-center rounded bg-primary py-2 px-6 font-medium text-gray hover:bg-opacity-90"
                      type="submit"
                    >
                      {t("save")}
                    </button>
                  </div>
              </form>
            </div>
          </div>
        </div>
      </div>
    </>
  );
};

export default Settings;
