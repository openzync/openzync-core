import type { Metadata } from "next";
import "@fontsource/roboto/300.css";
import "@fontsource/roboto/400.css";
import "@fontsource/roboto/500.css";
import "@fontsource/roboto/700.css";
import { AuthProvider } from "@/lib/auth/AuthContext";
import { MuiProvider } from "./MuiProvider";
import { NotificationProvider } from "@/components/shared/NotificationProvider";

export const metadata: Metadata = {
  title: "OpenZep Dashboard",
  description: "OpenZep — Agent Memory Infrastructure",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body style={{ margin: 0 }}>
        <MuiProvider>
          <NotificationProvider>
            <AuthProvider>{children}</AuthProvider>
          </NotificationProvider>
        </MuiProvider>
      </body>
    </html>
  );
}
