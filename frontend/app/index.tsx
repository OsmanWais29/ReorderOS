// Entry — redirects to the onboarding flow on first launch, otherwise to the
// main app. (For the scaffold we always start at /onboarding/welcome.)

import { Redirect } from 'expo-router';

export default function Index() {
  return <Redirect href="/onboarding/welcome" />;
}
