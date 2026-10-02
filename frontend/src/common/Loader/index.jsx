const Loader = () => {
  return (
    <div className="flex  items-center justify-center " role="status">
      <div className="relative h-16 w-16 text-primary">
        <svg
          className="absolute left-1/2 top-0 -ml-3 h-6 w-6 origin-top animate-drop-fall fill-current"
          viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path d="M12 2C12 2 5 10.5 5 15a7 7 0 0 0 14 0C19 10.5 12 2 12 2Z" />
        </svg>
        <div className="absolute bottom-0 left-1/2 -ml-5 h-2 w-10 animate-drop-ripple rounded-[50%] border-2 border-solid border-current"></div>
      </div>
    </div>
  );
};

export default Loader;
