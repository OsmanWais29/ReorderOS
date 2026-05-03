// Onboarding stack layout. Each step is a route under /onboarding/.
import { Stack } from 'expo-router';
import { T } from '@/theme/tokens';

export default function OnboardingLayout() {
  return (
    <Stack
      screenOptions={{
        headerShown: false,
        contentStyle: { backgroundColor: T.bg },
        animation: 'slide_from_right',
        gestureEnabled: true,
      }}
    />
  );
}
