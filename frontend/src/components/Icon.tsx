// Icon component — react-native-svg version of web Icon.jsx.
// Lucide-family, stroke 1.8, 20px default.
//
// Only the icons used in onboarding are ported here. Add more as needed —
// each entry is a function returning <Path/Polyline/etc> children.

import React from 'react';
import Svg, { Path, Polyline, Polygon, Line, Circle, Rect, type SvgProps } from 'react-native-svg';

type IconName =
  | 'logo' | 'maple-leaf'
  | 'check' | 'x' | 'chevron-right' | 'chevron-left' | 'chevron-down' | 'chevron-up'
  | 'plus' | 'minus' | 'arrow-right' | 'arrow-down' | 'arrow-up'
  | 'lock' | 'mail' | 'user' | 'users' | 'eye' | 'eye-off'
  | 'bell' | 'send' | 'shield' | 'truck' | 'package' | 'book' | 'flame'
  | 'trend-up' | 'sparkles' | 'settings' | 'home' | 'list'
  | 'search' | 'camera' | 'fingerprint' | 'pin'
  | 'message' | 'phone' | 'globe' | 'building';

type Props = {
  name: IconName;
  size?: number;
  color?: string;
  strokeWidth?: number;
  fill?: string;
} & Omit<SvgProps, 'fill' | 'color'>;

export function Icon({ name, size = 20, color = '#fff', strokeWidth = 1.8, fill = 'none', ...rest }: Props) {
  const common = {
    stroke: color,
    strokeWidth,
    strokeLinecap: 'round' as const,
    strokeLinejoin: 'round' as const,
    fill,
  };
  return (
    <Svg width={size} height={size} viewBox="0 0 24 24" {...rest}>
      {renderPath(name, common, color)}
    </Svg>
  );
}

function renderPath(name: IconName, p: any, color: string) {
  switch (name) {
    case 'logo':
      return (
        <>
          <Circle cx="12" cy="12" r="9" {...p}/>
          <Path d="M5 12c3-4 11-4 14 0" {...p}/>
          <Path d="M5 12c3 4 11 4 14 0" {...p}/>
          <Circle cx="16" cy="12" r="1.3" fill={color} stroke="none"/>
        </>
      );
    case 'maple-leaf':
      return <Path d="M12 2l-1 4-3-1 2 4-5 1 4 3-1 2 3-1 1 4 1-4 3 1-1-2 4-3-5-1 2-4-3 1z" {...p}/>;
    case 'check':
      return <Polyline points="20 6 9 17 4 12" {...p}/>;
    case 'x':
      return (
        <>
          <Line x1="18" y1="6" x2="6" y2="18" {...p}/>
          <Line x1="6" y1="6" x2="18" y2="18" {...p}/>
        </>
      );
    case 'chevron-right': return <Polyline points="9 18 15 12 9 6" {...p}/>;
    case 'chevron-left':  return <Polyline points="15 18 9 12 15 6" {...p}/>;
    case 'chevron-down':  return <Polyline points="6 9 12 15 18 9" {...p}/>;
    case 'chevron-up':    return <Polyline points="18 15 12 9 6 15" {...p}/>;
    case 'plus':          return (
      <>
        <Line x1="12" y1="5" x2="12" y2="19" {...p}/>
        <Line x1="5" y1="12" x2="19" y2="12" {...p}/>
      </>
    );
    case 'minus':         return <Line x1="5" y1="12" x2="19" y2="12" {...p}/>;
    case 'arrow-right':   return (
      <>
        <Line x1="5" y1="12" x2="19" y2="12" {...p}/>
        <Polyline points="12 5 19 12 12 19" {...p}/>
      </>
    );
    case 'arrow-down':    return (
      <>
        <Line x1="12" y1="5" x2="12" y2="19" {...p}/>
        <Polyline points="19 12 12 19 5 12" {...p}/>
      </>
    );
    case 'arrow-up':      return (
      <>
        <Line x1="12" y1="19" x2="12" y2="5" {...p}/>
        <Polyline points="5 12 12 5 19 12" {...p}/>
      </>
    );
    case 'lock':          return (
      <>
        <Rect x="3" y="11" width="18" height="11" rx="2" {...p}/>
        <Path d="M7 11V7a5 5 0 0 1 10 0v4" {...p}/>
      </>
    );
    case 'mail':          return (
      <>
        <Path d="M4 4h16c1.1 0 2 .9 2 2v12c0 1.1-.9 2-2 2H4c-1.1 0-2-.9-2-2V6c0-1.1.9-2 2-2z" {...p}/>
        <Polyline points="22,6 12,13 2,6" {...p}/>
      </>
    );
    case 'user':          return (
      <>
        <Path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2" {...p}/>
        <Circle cx="12" cy="7" r="4" {...p}/>
      </>
    );
    case 'users':         return (
      <>
        <Path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" {...p}/>
        <Circle cx="9" cy="7" r="4" {...p}/>
        <Path d="M23 21v-2a4 4 0 0 0-3-3.87" {...p}/>
        <Path d="M16 3.13a4 4 0 0 1 0 7.75" {...p}/>
      </>
    );
    case 'eye':           return (
      <>
        <Path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" {...p}/>
        <Circle cx="12" cy="12" r="3" {...p}/>
      </>
    );
    case 'eye-off':       return (
      <>
        <Path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" {...p}/>
        <Line x1="1" y1="1" x2="23" y2="23" {...p}/>
      </>
    );
    case 'bell':          return (
      <>
        <Path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" {...p}/>
        <Path d="M13.73 21a2 2 0 0 1-3.46 0" {...p}/>
      </>
    );
    case 'send':          return (
      <>
        <Line x1="22" y1="2" x2="11" y2="13" {...p}/>
        <Polygon points="22 2 15 22 11 13 2 9 22 2" {...p}/>
      </>
    );
    case 'shield':        return <Path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" {...p}/>;
    case 'truck':         return (
      <>
        <Rect x="1" y="3" width="15" height="13" {...p}/>
        <Polygon points="16 8 20 8 23 11 23 16 16 16 16 8" {...p}/>
        <Circle cx="5.5" cy="18.5" r="2.5" {...p}/>
        <Circle cx="18.5" cy="18.5" r="2.5" {...p}/>
      </>
    );
    case 'package':       return (
      <>
        <Path d="M16.5 9.4l-9-5.19" {...p}/>
        <Path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" {...p}/>
      </>
    );
    case 'book':          return (
      <>
        <Path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" {...p}/>
        <Path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" {...p}/>
      </>
    );
    case 'flame':         return (
      <Path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z" {...p}/>
    );
    case 'trend-up':      return (
      <>
        <Polyline points="22 7 13.5 15.5 8.5 10.5 2 17" {...p}/>
        <Polyline points="16 7 22 7 22 13" {...p}/>
      </>
    );
    case 'sparkles':      return (
      <>
        <Path d="M12 3L13.5 8.5 19 10 13.5 11.5 12 17 10.5 11.5 5 10 10.5 8.5 12 3z" {...p}/>
        <Path d="M19 3l.75 2.75L22 6.5l-2.25.75L19 10l-.75-2.75L16 6.5l2.25-.75L19 3z" {...p}/>
      </>
    );
    case 'settings':      return (
      <>
        <Circle cx="12" cy="12" r="3" {...p}/>
        <Path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" {...p}/>
      </>
    );
    case 'home':          return (
      <>
        <Path d="M3 9l9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" {...p}/>
        <Polyline points="9 22 9 12 15 12 15 22" {...p}/>
      </>
    );
    case 'list':          return (
      <>
        <Line x1="8" y1="6" x2="21" y2="6" {...p}/>
        <Line x1="8" y1="12" x2="21" y2="12" {...p}/>
        <Line x1="8" y1="18" x2="21" y2="18" {...p}/>
        <Circle cx="3" cy="6" r="0.5" stroke={color} fill={color}/>
        <Circle cx="3" cy="12" r="0.5" stroke={color} fill={color}/>
        <Circle cx="3" cy="18" r="0.5" stroke={color} fill={color}/>
      </>
    );
    case 'search':        return (
      <>
        <Circle cx="11" cy="11" r="8" {...p}/>
        <Line x1="21" y1="21" x2="16.65" y2="16.65" {...p}/>
      </>
    );
    case 'camera':        return (
      <>
        <Path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z" {...p}/>
        <Circle cx="12" cy="13" r="4" {...p}/>
      </>
    );
    case 'fingerprint':   return (
      <>
        <Path d="M6.5 17.5a8 8 0 0 1 11 0" {...p}/>
        <Path d="M4 14a12 12 0 0 1 16 0" {...p}/>
        <Path d="M9 12a6 6 0 0 1 6 0" {...p}/>
        <Circle cx="12" cy="10" r="2" {...p}/>
      </>
    );
    case 'pin':           return <Path d="M12 2l3 7h7l-5.5 4.5L18.5 21 12 16.5 5.5 21l2-7.5L2 9h7z" {...p}/>;
    case 'message':       return <Path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" {...p}/>;
    case 'phone':         return <Path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.95.37 1.88.7 2.77a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.31-1.31a2 2 0 0 1 2.11-.45c.89.33 1.82.57 2.77.7A2 2 0 0 1 22 16.92z" {...p}/>;
    case 'globe':         return (
      <>
        <Circle cx="12" cy="12" r="10" {...p}/>
        <Line x1="2" y1="12" x2="22" y2="12" {...p}/>
        <Path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" {...p}/>
      </>
    );
    case 'building':      return (
      <>
        <Rect x="4" y="2" width="16" height="20" rx="2" {...p}/>
        <Line x1="9" y1="22" x2="9" y2="18" {...p}/>
        <Line x1="15" y1="22" x2="15" y2="18" {...p}/>
      </>
    );
    default:
      return null;
  }
}

export type { IconName };
