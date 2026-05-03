import { Stub } from '@/components/Stub';
import { useLocalSearchParams } from 'expo-router';
export default function Connecting() {
  const { provider } = useLocalSearchParams<{ provider?: string }>();
  return <Stub step={5} title={`Connecting to ${provider ?? 'POS'}…`} nextHref="/onboarding/found-summary" note="OAuth handoff goes here. Use expo-auth-session for Square/Clover/Toast OAuth." />;
}
