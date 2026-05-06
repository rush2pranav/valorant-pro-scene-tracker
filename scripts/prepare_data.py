"""
Valorant Pro Scene Data Preparation
=====================================
Enriches VLR.gg match results and player stats for Power BI analysis. Also pulls live rankings from the unofficial VLR API.
"""

import pandas as pd
import numpy as np
import requests
import os
import time
import json

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data')
os.makedirs(DATA_DIR, exist_ok=True)

VLR_API = "https://vlrggapi.vercel.app"


def load_and_clean_results():
    """Load and enrich match results."""
    print("\n--- Processing Match Results ---")
    df = pd.read_csv(os.path.join(DATA_DIR, 'results.csv'), encoding='latin-1')
    df = df.drop(columns=['Unnamed: 0'], errors='ignore')
    
    # parse scores to numeric
    df['score1'] = pd.to_numeric(df['score1'], errors='coerce')
    df['score2'] = pd.to_numeric(df['score2'], errors='coerce')
    
    # fetermine winner
    df['winner'] = np.where(df['score1'] > df['score2'], df['team1'],
                   np.where(df['score2'] > df['score1'], df['team2'], 'Draw'))
    df['loser'] = np.where(df['score1'] > df['score2'], df['team2'],
                  np.where(df['score2'] > df['score1'], df['team1'], 'Draw'))
    
    # score differential
    df['score_diff'] = abs(df['score1'] - df['score2'])
    df['total_rounds'] = df['score1'] + df['score2']
    
    # match closeness
    df['is_close_match'] = df['score_diff'] <= 2
    df['is_stomp'] = df['score_diff'] >= 7
    
    # match type based on round_info
    df['match_format'] = df['round_info'].fillna('').apply(
        lambda x: 'Best of 5' if 'bo5' in str(x).lower() or '/5' in str(x)
        else ('Best of 3' if 'bo3' in str(x).lower() or '/3' in str(x)
        else 'Best of 1'))
    
    # parse time
    df['time_completed'] = pd.to_datetime(df['time_completed'], errors='coerce')
    df['match_year'] = df['time_completed'].dt.year
    df['match_month'] = df['time_completed'].dt.month
    df['match_day_of_week'] = df['time_completed'].dt.day_name()
    
    # tournament tier - simplified classification
    df['tournament_tier'] = df['tournament_name'].fillna('').apply(classify_tournament)
    
    print(f"  Loaded {len(df):,} matches")
    print(f"  Date range: {df['time_completed'].min()} to {df['time_completed'].max()}")
    print(f"  Unique teams: {pd.concat([df['team1'], df['team2']]).nunique()}")
    print(f"  Unique tournaments: {df['tournament_name'].nunique()}")
    
    return df


def classify_tournament(name):
    """Classify tournament tier based on name."""
    name_lower = name.lower()
    if any(x in name_lower for x in ['champions', 'masters', 'lock//in']):
        return 'International'
    elif any(x in name_lower for x in ['challengers', 'stage', 'kickoff', 'split']):
        return 'Regional League'
    elif 'game changers' in name_lower:
        return 'Game Changers'
    elif any(x in name_lower for x in ['ascension', 'promotion']):
        return 'Ascension'
    else:
        return 'Other'


def load_and_clean_stats():
    """Load and enrich player stats."""
    print("\n--- Processing Player Stats ---")
    df = pd.read_csv(os.path.join(DATA_DIR, 'stats.csv'), encoding='latin-1')
    df = df.drop(columns=['Unnamed: 0'], errors='ignore')
    
    # convert numeric columns
    numeric_cols = ['rds', 'average_combat_score', 'kill_deaths', 'average_damage_per_round',
                    'kills_per_round', 'assists_per_round', 'first_kills_per_round',
                    'first_deaths_per_round', 'headshot_percentage', 'clutch_success_percentage',
                    'total_kills', 'total_deaths', 'total_assists', 'total_first_kills', 'total_first_deaths']
    
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace('%', ''), errors='coerce')
    
    # parse K/D ratio
    df['kd_ratio'] = df['kill_deaths'].fillna(0)
    
    # performance tier
    df['performance_tier'] = pd.cut(
        df['average_combat_score'].fillna(0),
        bins=[0, 150, 200, 230, 260, 999],
        labels=['Below Average (<150)', 'Average (150-199)', 'Good (200-229)', 
                'Great (230-259)', 'Elite (260+)']
    )
    
    # impact score (composite)
    df['impact_score'] = (
        df['average_combat_score'].fillna(0) * 0.3 +
        df['kills_per_round'].fillna(0) * 100 * 0.2 +
        df['average_damage_per_round'].fillna(0) * 0.2 +
        df['first_kills_per_round'].fillna(0) * 100 * 0.15 +
        df['headshot_percentage'].fillna(0) * 0.15
    ).round(1)
    
    # role classification based on stats
    df['likely_role'] = df.apply(classify_role, axis=1)
    
    print(f"  Loaded {len(df):,} player stat records")
    print(f"  Unique players: {df['player'].nunique()}")
    print(f"  Unique agents: {df['agent'].nunique()}")
    print(f"  Regions: {df['region'].nunique()}")
    
    return df


def classify_role(row):
    """Guess player role based on stat patterns."""
    fk = row.get('first_kills_per_round', 0) or 0
    fd = row.get('first_deaths_per_round', 0) or 0
    acs = row.get('average_combat_score', 0) or 0
    assists = row.get('assists_per_round', 0) or 0
    
    if fk > 0.15:
        return 'Entry Fragger'
    elif assists > 0.5:
        return 'Support'
    elif acs > 230:
        return 'Star Player'
    else:
        return 'Flex'


def build_team_rankings(results):
    """Build team performance rankings from match results."""
    print("\n--- Building Team Rankings ---")
    
    # get all team appearances
    team_stats = []
    all_teams = pd.concat([results['team1'], results['team2']]).unique()
    
    for team in all_teams:
        if pd.isna(team) or team == 'Draw':
            continue
            
        matches = results[(results['team1'] == team) | (results['team2'] == team)]
        wins = len(results[results['winner'] == team])
        losses = len(matches) - wins - len(matches[matches['winner'] == 'Draw'])
        
        # calculate rounds won/lost
        t1_matches = results[results['team1'] == team]
        t2_matches = results[results['team2'] == team]
        rounds_won = t1_matches['score1'].sum() + t2_matches['score2'].sum()
        rounds_lost = t1_matches['score2'].sum() + t2_matches['score1'].sum()
        
        # tournaments participated
        tournaments = matches['tournament_name'].nunique()
        
        # recent form (last 10 matches)
        recent = matches.sort_values('time_completed', ascending=False).head(10)
        recent_wins = len(recent[recent['winner'] == team])
        
        team_stats.append({
            'team': team,
            'total_matches': len(matches),
            'wins': wins,
            'losses': losses,
            'win_rate': round(wins / max(len(matches), 1) * 100, 1),
            'rounds_won': int(rounds_won),
            'rounds_lost': int(rounds_lost),
            'round_diff': int(rounds_won - rounds_lost),
            'tournaments': tournaments,
            'recent_form_wins': recent_wins,
            'recent_form_total': len(recent),
        })
    
    df_teams = pd.DataFrame(team_stats)
    df_teams = df_teams.sort_values('win_rate', ascending=False)
    
    # add tier based on win rate + matches played
    df_teams['team_tier'] = df_teams.apply(
        lambda r: 'S-Tier' if r['win_rate'] >= 65 and r['total_matches'] >= 20
        else ('A-Tier' if r['win_rate'] >= 55 and r['total_matches'] >= 15
        else ('B-Tier' if r['win_rate'] >= 45
        else 'C-Tier')), axis=1)
    
    print(f"  Ranked {len(df_teams)} teams")
    print(f"  S-Tier: {len(df_teams[df_teams['team_tier'] == 'S-Tier'])}")
    print(f"  A-Tier: {len(df_teams[df_teams['team_tier'] == 'A-Tier'])}")
    
    return df_teams


def fetch_live_rankings():
    """Pull current rankings from VLR API."""
    print("\n--- Fetching Live Rankings ---")
    regions = ['na', 'eu', 'ap', 'br', 'kr', 'jp', 'la']
    all_rankings = []
    
    for region in regions:
        try:
            url = f"{VLR_API}/rankings/{region}"
            print(f"  Fetching {region.upper()} rankings...")
            resp = requests.get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                segments = data.get('data', {}).get('segments', data.get('data', []))
                if isinstance(segments, list):
                    for team in segments:
                        if isinstance(team, dict):
                            all_rankings.append({
                                'team': team.get('team', ''),
                                'region': region.upper(),
                                'ranking': team.get('ranking', ''),
                                'record': team.get('record', ''),
                                'earnings': team.get('earnings', ''),
                            })
            time.sleep(0.5)
        except Exception as e:
            print(f"  Warning: Failed to fetch {region}: {e}")
    
    if all_rankings:
        df = pd.DataFrame(all_rankings)
        print(f"  Fetched {len(df)} team rankings across {len(regions)} regions")
        return df
    else:
        print("  No live rankings available - using offline data only")
        return pd.DataFrame()


def main():
    print("=" * 60)
    print("VALORANT PRO SCENE - DATA PREPARATION")
    print("=" * 60)
    
    # process match results
    results = load_and_clean_results()
    results.to_csv(os.path.join(DATA_DIR, 'results_enriched.csv'), index=False)
    
    # process player stats
    stats = load_and_clean_stats()
    stats.to_csv(os.path.join(DATA_DIR, 'stats_enriched.csv'), index=False)
    
    # build team rankings
    teams = build_team_rankings(results)
    teams.to_csv(os.path.join(DATA_DIR, 'team_rankings.csv'), index=False)
    
    # fetch live rankings
    live_rankings = fetch_live_rankings()
    if not live_rankings.empty:
        live_rankings.to_csv(os.path.join(DATA_DIR, 'live_rankings.csv'), index=False)
    
    print(f"\n=== OUTPUT SUMMARY ===")
    print(f"  results_enriched.csv: {len(results):,} matches")
    print(f"  stats_enriched.csv:   {len(stats):,} player records")
    print(f"  team_rankings.csv:    {len(teams):,} teams")
    if not live_rankings.empty:
        print(f"  live_rankings.csv:    {len(live_rankings):,} rankings")
    
    print(f"\n  Match format distribution:")
    print(results['match_format'].value_counts().to_string())
    print(f"\n  Tournament tiers:")
    print(results['tournament_tier'].value_counts().to_string())
    print(f"\n  Top 10 teams by win rate (min 20 matches):")
    top = teams[teams['total_matches'] >= 20].head(10)
    print(top[['team', 'total_matches', 'wins', 'losses', 'win_rate', 'team_tier']].to_string(index=False))
    print(f"\n  Agent pick distribution:")
    print(stats['agent'].value_counts().to_string())
    print(f"\n  Player performance tiers:")
    print(stats['performance_tier'].value_counts().to_string())


if __name__ == '__main__':
    main()