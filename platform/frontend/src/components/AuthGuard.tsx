import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { isLoggedIn, tryRefresh } from '@/lib/auth';

/**
 * Route guard: redirects to /login if not authenticated.
 * On mount, tries silent refresh if a refresh token exists.
 */
export function AuthGuard({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    (async () => {
      if (isLoggedIn()) {
        const ok = await tryRefresh();
        if (!ok && !isLoggedIn()) {
          navigate('/login', { replace: true });
          return;
        }
      } else {
        navigate('/login', { replace: true });
        return;
      }
      setChecking(false);
    })();
  }, [navigate]);

  if (checking) {
    return (
      <div className="flex h-screen items-center justify-center bg-ide-bg">
        <div className="text-gray-500 text-sm">Loading...</div>
      </div>
    );
  }

  return <>{children}</>;
}
