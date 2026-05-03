// Main app tab layout — Home / Stock / Orders / Sales / More.
// Placeholder screens; port full versions from app/MainApp.jsx.

import React from 'react';
import { Tabs } from 'expo-router';
import { Icon, type IconName } from '@/components/Icon';
import { T, TYPE } from '@/theme/tokens';

export default function AppLayout() {
  return (
    <Tabs
      screenOptions={{
        tabBarActiveTintColor: T.ac,
        tabBarInactiveTintColor: T.ter,
        tabBarStyle: {
          backgroundColor: T.elev1,
          borderTopColor: T.hairline,
          borderTopWidth: 0.5,
          height: 84,
          paddingTop: 6,
        },
        tabBarLabelStyle: { ...TYPE.caption2, fontWeight: '600' },
        headerStyle: { backgroundColor: T.bg },
        headerTitleStyle: { color: T.text, ...TYPE.headline },
        headerShadowVisible: false,
      }}
    >
      <Tabs.Screen name="home"   options={tab('Home',   'home')} />
      <Tabs.Screen name="stock"  options={tab('Stock',  'package')} />
      <Tabs.Screen name="orders" options={tab('Orders', 'truck')} />
      <Tabs.Screen name="sales"  options={tab('Sales',  'trend-up')} />
      <Tabs.Screen name="more"   options={tab('More',   'list')} />
    </Tabs>
  );
}

function tab(title: string, iconName: IconName) {
  return {
    title,
    tabBarIcon: ({ color, size }: { color: string; size: number }) => (
      <Icon name={iconName} size={size ?? 22} color={color} />
    ),
  };
}
