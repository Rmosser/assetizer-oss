import { createRoot } from "react-dom/client";
import App from "./App";
import { startBasket } from "./basket";
import "@fontsource/noto-serif-sc/400.css";
import "@fontsource/noto-serif-sc/700.css";
import "./styles.css";

startBasket();
createRoot(document.getElementById("root")!).render(<App />);
